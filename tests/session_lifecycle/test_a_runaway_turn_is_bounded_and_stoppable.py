"""A model that never stops calling tools: igni bounds the turn, and Stop always ends it."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator

import pytest

from tests.session_lifecycle.invariant_checker import InvariantChecker
from tests.session_lifecycle.lifecycle_rig import RIG_CONFIG, LifecycleRig
from tests.session_lifecycle.schemas.model_turn import ModelTurn
from tests.session_lifecycle.schemas.scripted_tool_call import ScriptedToolCall
from tests.session_lifecycle.verdict import Verdict

pytestmark = [pytest.mark.session_lifecycle]

STOPPED = "[runaway stopped]"
UNATTENDED = "[runaway unattended]"
# Model calls one unattended turn may make before it counts as unbounded.
BOUND = 40
# Runaway turns stopped one after another, each at a later call.
ROUNDS = 5
LOOP = ModelTurn(
    text="One more look.",
    tool_calls=[ScriptedToolCall(name="run_shell_command", arguments={"command": "sleep 0"})],
)


@pytest.fixture(scope="module")
def rig(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LifecycleRig]:
    rig = LifecycleRig(tmp_path_factory.mktemp("runaway"), extra_config=RIG_CONFIG)
    for mark in (STOPPED, UNATTENDED):
        rig.script.add(f"keep looking {mark}", then=LOOP)
        rig.script.add(f"probe {mark}", ModelTurn(text="Answered."))
    rig.start()
    try:
        yield rig
    finally:
        rig.stop()


def _calls(rig: LifecycleRig, mark: str) -> int:
    return sum(1 for r in rig.model.agent_requests() if r.mentions(mark))


async def test_stop_ends_a_runaway_turn_and_nothing_follows(
    rig: LifecycleRig, request: pytest.FixtureRequest
) -> None:
    """Stopped at a different call each round, since where Stop lands is what can go wrong."""
    cell = "runaway.stop"
    Verdict.expect_known(request, cell)
    violations = []
    for round_no in range(ROUNDS):
        mark = f"{STOPPED} #{round_no}"
        checker = InvariantChecker(rig, mark)
        client = await rig.new_session(f"runaway-stop-{round_no}")
        try:
            checker.expect(f"keep looking {mark}")
            await client.send(f"keep looking {mark}")
            calls = 6 + round_no
            await asyncio.to_thread(
                rig.model.wait_for, lambda rs, m=mark, n=calls: _calls(rig, m) >= n, 30
            )
            frames = len(client.frames)
            cancelled_at = time.monotonic()
            await client.stop()
            checker.mark("cancelled")
            await checker.check_cancelled("Stop in a runaway turn", client, cancelled_at, frames)
            checker.check_requests("Stop in a runaway turn")
            probe = await client.send(f"probe {mark}")
            checker.expect(f"probe {mark}")
            try:
                await client.wait_for(
                    lambda f, p=probe: f.type == "stream_end" and f.id == p, since=0, timeout=25
                )
            except TimeoutError:
                checker.fail("answers", "probe", "the next message was never answered")
        finally:
            await client.stop()
            await rig.close_windows()
        violations.extend(checker.violations)
    Verdict.settle(cell, violations)


async def test_a_runaway_turn_left_alone_is_ended_by_a_limit(
    rig: LifecycleRig, request: pytest.FixtureRequest
) -> None:
    cell = "runaway.unattended"
    Verdict.expect_known(request, cell)
    checker = InvariantChecker(rig, UNATTENDED)
    client = await rig.new_session("runaway-alone")
    try:
        sent = await client.send(f"keep looking {UNATTENDED}")
        deadline = time.monotonic() + 90
        ended = False
        while time.monotonic() < deadline and _calls(rig, UNATTENDED) < BOUND:
            if any(f.type == "stream_end" and f.id == sent for f in client.frames):
                ended = True
                break
            await asyncio.sleep(0.2)
        if not ended:
            checker.fail(
                "bounded-run",
                "a runaway turn left alone",
                f"the turn was still calling tools after {_calls(rig, UNATTENDED)} model calls; "
                "nothing in igni ended it",
            )
    finally:
        await client.stop()
        await asyncio.sleep(1.0)
        await rig.close_windows()
    Verdict.settle(cell, checker.violations)
