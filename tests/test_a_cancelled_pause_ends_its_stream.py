"""A cancel that ends a paused run ends its stream, as for a running one.

``cancel_run`` answers ``cancelled: true`` for a run that is working and
for one paused on an approval (``tests/test_cancel_ends_a_paused_run.py``).
A working run's stream then ends with an ``info`` saying it was cancelled
and a ``stream_end``; a paused run's had already ended, and nothing more
came. A client cannot tell the two answers apart, so one that follows the
cancel to its ``stream_end`` (igni-client's ``async with session.cancel()``)
waited out its timeout. Now both answers are followed by the same two
frames, and an answer of ``false`` still by none.

Every test runs the orchestrator the app builds over a real backend —
only the team is replaced — and reads what the windows were sent.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from agno.models.response import ToolExecution
from agno.run.agent import RunContentEvent, RunPausedEvent
from agno.run.requirement import RunRequirement

from igni_core.backend.server import BackendServer
from igni_core.core.config.settings_loader import SettingsLoader
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod
from igni_core.protocol.schemas.envelope import Standing
from tests.host_model_helpers import HostRig

#: What a cancelled run's stream says (``RunController.cancelled_info``).
CANCELLED_BY_USER = "Run cancelled by user."


class _Team:
    """A team whose run pauses on a shell call — or, ``working``, runs on."""

    instructions: str | None = None
    db: Any = None

    def __init__(self, *, working: bool = False) -> None:
        self.working = working

    async def arun(self, *_: Any, **__: Any) -> AsyncIterator[Any]:
        if self.working:
            yield RunContentEvent(content="working", run_id="run-1")
            await asyncio.sleep(3600)
        yield RunPausedEvent(
            run_id="run-1",
            requirements=[
                RunRequirement(
                    tool_execution=ToolExecution(
                        tool_call_id="call-1",
                        tool_name="run_shell_command",
                        tool_args={"command": "make deploy"},
                        requires_confirmation=True,
                    )
                )
            ],
        )

    async def acontinue_run(self, **_: Any) -> AsyncIterator[Any]:
        return
        yield


class _RealBackendRig(HostRig):
    """The app's orchestrator and host model, over a real boot backend."""

    def __init__(self, tmp_path: Path, team: _Team, resume: str | None = None) -> None:
        super().__init__(tmp_path)
        backend = BackendServer(
            SettingsLoader.load(project_dir=self.project),
            project_dir=self.project,
            resume_session_id=resume,
        )
        backend._session.main_team = team  # type: ignore[assignment]
        self.backend = backend
        self.session = backend  # type: ignore[assignment]


def _waiting(backend: BackendServer) -> list[str]:
    return [
        r.requirement_id
        for m in backend.controllers.hitl.waiting_messages()
        for r in m.requirements
    ]


async def _send(rig: _RealBackendRig, message: msg.Message) -> None:
    message.mark_from("ws-1", Standing.WINDOW)
    assert rig.orchestrator is not None
    await rig.orchestrator.dispatch(message)


def _cancel_run(request_id: str) -> msg.RPCRequest:
    return msg.RPCRequest(id=request_id, method=RpcMethod.CANCEL_RUN.value, args={})


def _ends(frames: list[msg.Message]) -> list[tuple[str, str]]:
    """The cancel's ``info`` lines and ``stream_end`` frames, in order, by type and text."""
    return [
        (m.type, getattr(m, "text", ""))
        for m in frames
        if isinstance(m, msg.StreamEnd) or (isinstance(m, msg.Info) and m.text == CANCELLED_BY_USER)
    ]


async def _cancel(rig: _RealBackendRig, request_id: str) -> tuple[bool, list[msg.Message]]:
    """``cancel_run`` from the window: its answer, and what was sent after it."""
    start = len(rig.windows.sent)
    await _send(rig, _cancel_run(request_id))
    await rig.settle()
    sent = rig.windows.sent[start:]
    [answer] = [m for m in sent if isinstance(m, msg.RPCResponse) and m.id == request_id]
    assert answer.error is None, answer.error
    return answer.result["cancelled"], sent


@pytest.fixture
async def paused(tmp_path: Path):
    """A rig whose boot session has a run paused on one approval."""
    rig = await _RealBackendRig(tmp_path, _Team()).start()
    await _send(rig, msg.UserMessage(id="m-1", text="deploy it"))
    assert len(_waiting(rig.backend)) == 1
    try:
        yield rig
    finally:
        await rig.stop()


_ENDED = [("info", CANCELLED_BY_USER), ("stream_end", "")]


# ── A paused run ──────────────────────────────────────────────────


async def test_cancelling_a_paused_run_streams_an_info_then_a_stream_end(paused) -> None:
    cancelled, sent = await _cancel(paused, "c-1")

    assert cancelled is True
    assert _ends(sent) == _ENDED
    assert {m.session_id for m in sent if m.type in ("info", "stream_end")} == {
        paused.backend.session_id
    }, "the session's own, as igni-client follows them"
    assert _waiting(paused.backend) == []


async def test_the_cancel_message_ends_the_paused_stream_too(paused) -> None:
    start = len(paused.windows.sent)

    await _send(paused, msg.Cancel(id="c-2"))
    await paused.settle()

    assert _ends(paused.windows.sent[start:]) == _ENDED
    assert _waiting(paused.backend) == []


async def test_an_extension_s_chat_cancel_ends_the_paused_stream_too(paused) -> None:
    ext = paused.install("acme.reviewer", host=["chat.control", "chat.approvals"])
    start = len(paused.windows.sent)

    reply = await paused.ask(ext, "call", "chat.cancel")
    await paused.settle()

    assert reply.ok, reply.error
    assert _ends(paused.windows.sent[start:]) == _ENDED


async def test_a_second_cancel_finds_nothing_and_streams_nothing(paused) -> None:
    await _cancel(paused, "c-1")

    cancelled, sent = await _cancel(paused, "c-2")

    assert cancelled is False
    assert _ends(sent) == []


# ── A running run, as before ──────────────────────────────────────


async def test_cancelling_a_running_run_still_streams_one_info_and_one_stream_end(
    tmp_path: Path,
) -> None:
    rig = await _RealBackendRig(tmp_path, _Team(working=True)).start()
    try:
        assert rig.orchestrator is not None
        message = msg.UserMessage(id="m-1", text="work")
        message.mark_from("ws-1", Standing.WINDOW)
        running = rig.orchestrator.spawn_dispatch(message)
        for _ in range(500):
            if any(isinstance(m, msg.ContentDelta) for m in rig.windows.sent):
                break
            await asyncio.sleep(0.01)

        start = len(rig.windows.sent)
        cancelled, _ = await _cancel(rig, "c-1")
        await asyncio.wait_for(running, 5)
        await rig.settle()
        sent = rig.windows.sent[start:]

        assert cancelled is True
        assert _ends(sent) == _ENDED
        assert {m.id for m in sent if m.type in ("info", "stream_end")} == {"m-1"}, (
            "the frames of the turn it stopped"
        )
    finally:
        await rig.stop()


# ── Nothing to cancel ─────────────────────────────────────────────


async def test_cancelling_an_idle_session_answers_false_and_streams_nothing(
    tmp_path: Path,
) -> None:
    rig = await _RealBackendRig(tmp_path, _Team()).start()
    try:
        cancelled, sent = await _cancel(rig, "c-1")

        assert cancelled is False
        assert _ends(sent) == []
    finally:
        await rig.stop()


# ── After a restart ───────────────────────────────────────────────


async def test_cancelling_a_pause_a_previous_backend_left_ends_its_stream_too(
    tmp_path: Path,
) -> None:
    """No paused message in this process names the run; the approvals the
    restarted backend asks again are what the cancel ends."""
    before = await _RealBackendRig(tmp_path, _Team()).start()
    try:
        await _send(before, msg.UserMessage(id="m-1", text="deploy it"))
        (requirement_id,) = _waiting(before.backend)
        session_id = before.backend.session_id
    finally:
        await before.stop()

    after = await _RealBackendRig(tmp_path, _Team(), resume=session_id).start()
    try:
        assert _waiting(after.backend) == [requirement_id], "asked again"

        cancelled, sent = await _cancel(after, "c-1")

        assert cancelled is True
        assert _ends(sent) == _ENDED
        assert _waiting(after.backend) == []
    finally:
        await after.stop()
