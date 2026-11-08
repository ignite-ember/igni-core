"""igni-client drives the real backend, over its real socket.

`igni-client` is tested against `FakeIgni` in its own package, which is a
model of igni and so can agree with a client that is wrong. Here nothing
is a model but the model: the real `BackendApp` boots in this process,
publishes its lockfile, and `igni-client` finds it from that file and
drives it as an outside program would — a session, a prompt streamed to
its end, an approval answered in the loop and the tool actually running,
the session list, the model catalogue, and a raw call.

Only the language model is replaced, by a script, so the run is
deterministic and needs no network (the suite's egress guard holds that).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from agno.models.base import Model
from agno.models.message import Message
from agno.models.response import ModelResponse

from igni_core.backend.app import BackendApp
from igni_core.backend.schemas_lifecycle import BackendLaunch
from igni_core.core.config.models import ModelRegistry
from tests.siblings import IGNI_SDK, needs_sdk, skip_module_without_sdk

skip_module_without_sdk()
pytestmark = needs_sdk
sys.path.insert(0, str(IGNI_SDK / "packages" / "igni-client" / "src"))

from igni_client import (  # noqa: E402 — the SDK is a package of its own, not installed here
    ApprovalRequested,
    ContentDelta,
    IgniClient,
    Local,
    NotAllowed,
    RunCompleted,
    RunError,
    ToolCompleted,
)

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="the loop's signal handlers")

Script = Callable[[list[Message]], ModelResponse]


class ScriptedModel(Model):
    """A model that answers from a script of the conversation so far."""

    def __init__(self, script: Script) -> None:
        super().__init__(id="scripted", provider="test")
        self._script = script

    def _answer(self, messages: list[Message]) -> ModelResponse:
        response = self._script(messages)
        response.role = "assistant"
        return response

    async def ainvoke_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ModelResponse]:
        yield self._answer(kwargs["messages"])

    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        return self._answer(kwargs["messages"])

    def invoke(self, *args: Any, **kwargs: Any) -> ModelResponse:  # pragma: no cover
        raise NotImplementedError

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        return response

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return response


def _script(messages: list[Message]) -> ModelResponse:
    """Say hello; asked to make a file, ask the shell for it, then say so.

    Only this turn counts — what came after the latest prompt — since the
    session's history carries earlier turns' tool results too.
    """
    last_prompt = max((i for i, m in enumerate(messages) if m.role == "user"), default=-1)
    turn = messages[last_prompt + 1 :]
    if any(m.role == "tool" for m in turn):
        return ModelResponse(content="Created done.txt.")
    last = str(messages[last_prompt].content) if last_prompt >= 0 else ""
    if "make a file" in last:
        return ModelResponse(
            tool_calls=[
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "run_shell_command",
                        "arguments": json.dumps({"command": "touch done.txt"}),
                    },
                }
            ]
        )
    return ModelResponse(content="hello from the scripted model")


@contextlib.contextmanager
def _signal_handlers_restored():
    loop = asyncio.get_running_loop()
    try:
        yield
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGUSR1", None)):
            if sig is not None:
                loop.remove_signal_handler(sig)


async def _until(predicate, timeout: float = 30) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("condition never held")
        await asyncio.sleep(0.02)


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / ".igni").mkdir()
    monkeypatch.delenv("IGNI_PARENT_PID", raising=False)
    monkeypatch.delenv("IGNI_WS_TOKEN", raising=False)
    monkeypatch.setattr(ModelRegistry, "get_model", lambda self, name=None: ScriptedModel(_script))
    monkeypatch.setattr(ModelRegistry, "get_context_window", lambda self, name=None: 200_000)
    return tmp_path


@POSIX_ONLY
async def test_igni_client_drives_a_session_on_the_real_backend(project: Path) -> None:
    app = BackendApp(BackendLaunch(ws_port=0, project_dir=project))
    with _signal_handlers_restored():
        running = asyncio.create_task(app.run())
        try:
            await _until(lambda: app.serving_port is not None or running.done())
            assert not running.done(), running.exception()

            async with IgniClient(Local(project_dir=project)) as igni:
                assert igni.client_id.startswith("ws-")
                session = await igni.sessions.create()

                # An approval, answered in the loop: the tool really runs.
                #
                # It is the session's first run on purpose. After a session's
                # first completed run igni names it in the background, and that
                # save writes back the session as it read it — erasing a run
                # that paused for approval in between ("No runs found for run
                # ID …" on resume). That is igni's race, not the client's; here
                # the approval comes before any naming can be in flight.
                asked: list[ApprovalRequested] = []
                results: list[ToolCompleted] = []
                errors: list[str] = []
                async for event in session.send("make a file"):
                    if isinstance(event, ApprovalRequested):
                        asked.append(event)
                        await event.approve()
                    elif isinstance(event, ToolCompleted):
                        results.append(event)
                    elif isinstance(event, RunError):
                        errors.append(event.error)
                assert [r.tool_name for r in asked[0].requirements] == ["run_shell_command"]
                assert not errors, errors
                ran = [(r.is_error, r.is_rejected, r.full_result or r.summary) for r in results]
                assert (project / "done.txt").exists(), f"the tool's result: {ran}"

                # A prompt, streamed to its end.
                streamed: list[Any] = [event async for event in session.send("say hello")]
                text = "".join(
                    e.text for e in streamed if isinstance(e, ContentDelta) and not e.is_thinking
                )
                assert "hello from the scripted model" in text
                assert any(isinstance(e, RunCompleted) for e in streamed)

                # The rest of the surface, as igni answers it.
                listed = await igni.sessions.list()
                assert session.id in [entry.session_id for entry in listed]
                catalogue = await igni.models.list()
                assert isinstance(catalogue.models, dict)
                assert isinstance(await igni.rpc("get_session_id"), str)
        finally:
            if not running.done():
                app.request_shutdown()
                await asyncio.wait_for(running, 30)


async def _approve_the_file(igni: IgniClient) -> None:
    session = await igni.sessions.create()
    async for event in session.send("make a file"):
        if isinstance(event, ApprovalRequested):
            await event.approve()


@POSIX_ONLY
async def test_a_program_approves_only_with_the_token_the_window_made(project: Path) -> None:
    # Started as the desktop app starts it: with a launch secret, which only
    # igni's window holds. The lockfile carries a program's token instead.
    app = BackendApp(BackendLaunch(ws_port=0, project_dir=project, ws_token="the-launch-secret"))
    with _signal_handlers_restored():
        running = asyncio.create_task(app.run())
        try:
            await _until(lambda: app.serving_port is not None or running.done())
            assert not running.done(), running.exception()
            port = app.serving_port
            assert port is not None

            # Through the lockfile — as the agent could — it runs, but approves nothing.
            async with IgniClient(Local(project_dir=project)) as igni:
                with pytest.raises(NotAllowed, match="approver token"):
                    await _approve_the_file(igni)
                with pytest.raises(Exception, match="only igni's window"):
                    await igni.rpc("mint_approver_token")
            assert not (project / "done.txt").exists()

            # igni's window makes an approver token; with it, the program approves.
            window = Local(url=f"ws://127.0.0.1:{port}", token="the-launch-secret")
            async with IgniClient(window) as igni:
                token = (await igni.rpc("mint_approver_token"))["token"]
            async with IgniClient(Local(project_dir=project, approver_token=token)) as igni:
                await _approve_the_file(igni)
            assert (project / "done.txt").exists()
        finally:
            if not running.done():
                app.request_shutdown()
                await asyncio.wait_for(running, 30)
