"""A few real multi-turn sessions against a real model: the live tier."""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from tests.session_lifecycle.invariant_checker import InvariantChecker
from tests.session_lifecycle.lifecycle_client import LifecycleClient
from tests.session_lifecycle.lifecycle_rig import LifecycleRig
from tests.session_lifecycle.verdict import Verdict

LIVE_CONFIG = os.environ.get("IGNI_LIFECYCLE_LIVE_CONFIG", "")
LLM_KEY = os.environ.get("IGNI_TEST_LLM_API_KEY", "")
LLM_URL = os.environ.get("IGNI_TEST_LLM_BASE_URL", "") or "https://api.openai.com/v1"
LLM_MODEL = os.environ.get("IGNI_TEST_LLM_MODEL", "")

pytestmark = [
    pytest.mark.session_lifecycle,
    pytest.mark.skipif(
        not (LIVE_CONFIG or (LLM_KEY and LLM_MODEL)),
        reason="no live model: set IGNI_LIFECYCLE_LIVE_CONFIG or IGNI_TEST_LLM_API_KEY",
    ),
]


def _models_yaml() -> str:
    """The ``models:`` block for the live model, its key carried over without being shown."""
    if LIVE_CONFIG:
        config = yaml.safe_load(Path(LIVE_CONFIG).expanduser().read_text()) or {}
        models = config.get("models") or {}
        default = models.get("default")
        entry = (models.get("registry") or {}).get(default)
        if not isinstance(entry, dict):
            pytest.skip(f"{LIVE_CONFIG} names no default model with a registry entry")
        registry = {"live": entry}
    else:
        registry = {
            "live": {
                "provider": "openai_like",
                "model_id": LLM_MODEL,
                "url": LLM_URL,
                "api_key_env": "IGNI_TEST_LLM_API_KEY",
            }
        }
    return yaml.safe_dump({"models": {"default": "live", "registry": registry}})


@pytest.fixture(scope="module")
def rig(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LifecycleRig]:
    rig = LifecycleRig(tmp_path_factory.mktemp("live"), models_yaml=_models_yaml())
    rig.start()
    try:
        yield rig
    finally:
        rig.stop()


async def _reply(client: LifecycleClient, request_id: str, timeout: float = 180) -> str:
    """The reply's text once its stream ends; a tool the model asks for on the way is refused."""
    for _ in range(5):
        await client.wait_for(
            lambda f, rid=request_id: f.type == "stream_end" and f.id == rid,
            since=0,
            timeout=timeout,
        )
        if not any(f.type == "run_paused" and f.id == request_id for f in client.frames):
            break
        request_id = await client.answer("reject", "once")
    return "".join(
        f.text
        for f in client.frames
        if f.id == request_id and f.type == "content_delta" and not f.data.get("is_thinking")
    )


async def test_a_fact_from_an_earlier_turn_is_remembered(rig: LifecycleRig) -> None:
    client = await rig.new_session("live-memory")
    code = f"BLUEFIN-{secrets.token_hex(3).upper()}"
    try:
        first = await client.send(
            f"My project codename is {code}. Reply with just OK, and use no tools."
        )
        await _reply(client, first)
        await client.quiet()
        second = await client.send(
            "What is my project codename? Answer with the codename only, and use no tools."
        )
        assert code in await _reply(client, second)
    finally:
        await rig.close_windows()


async def test_a_follow_up_while_an_approval_waits_knows_the_paused_turn(
    rig: LifecycleRig, request: pytest.FixtureRequest
) -> None:
    cell = "live.paused_follow_up"
    Verdict.expect_known(request, cell)
    checker = InvariantChecker(rig, "[live]")
    client = await rig.new_session("live-paused")
    word = f"marmalade-{secrets.token_hex(3)}"
    try:
        await client.send(f"Run the shell command `echo {word}` and tell me what it printed.")
        await client.wait_for(lambda f: f.type == "run_paused", since=0, timeout=180)
        await client.quiet()
        follow = await client.follow_up(
            "Before you run it: which exact shell command were you about to run? "
            "Answer with the command only, and do not run anything."
        )
        reply = await _reply(client, follow)
        if word not in reply:
            checker.fail(
                "history",
                "follow-up while paused",
                f"the reply did not know the paused command; it said {reply[:200]!r}",
            )
    finally:
        await client.stop()
        await rig.close_windows()
    Verdict.settle(cell, checker.violations)


async def test_stop_while_a_reply_streams_stops_it(rig: LifecycleRig) -> None:
    checker = InvariantChecker(rig, "[live]")
    client = await rig.new_session("live-stop")
    try:
        await client.send("Write a 1500-word essay on the history of the printing press.")
        await client.wait_for(lambda f: f.type == "content_delta", since=0, timeout=180)
        mark = len(client.frames)
        cancelled_at = time.monotonic()
        await client.stop()
        await checker.check_cancelled("Stop while streaming", client, cancelled_at, mark)
    finally:
        await rig.close_windows()
    assert not checker.violations, "\n".join(str(v) for v in checker.violations)


async def test_an_approved_command_s_output_reaches_the_reply(rig: LifecycleRig) -> None:
    client = await rig.new_session("live-approve")
    word = f"quince-{secrets.token_hex(3)}"
    try:
        await client.send(
            f"Run the shell command `echo {word}` and tell me exactly what it printed."
        )
        await client.wait_for(lambda f: f.type == "run_paused", since=0, timeout=180)
        await client.quiet()
        approved = await client.answer("confirm", "once")
        assert word in await _reply(client, approved)
        assert (await client.processing()) is False
    finally:
        await rig.close_windows()
