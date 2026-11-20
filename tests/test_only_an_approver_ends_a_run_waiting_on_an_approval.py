"""Only who may answer an approval may end the run waiting on it.

Cancelling a run paused on an approval drops the approval
(``tests/test_cancel_ends_a_paused_run.py``): that is refusing it, and a
program decides nothing for the person (``backend/standing_policy.py``).
So while a session waits on the person, ``cancel_run`` and ``cancel``
take the standing an answer takes — igni's window, or a program the
window made an approver. A run that is working stays anybody's to stop,
as it was.

The extension's ``chat.cancel`` (``chat.control``) is held to the same
rule by its grant: ending a waiting run needs ``chat.approvals``, the
grant that answers one. A service never cancels a waiting run at all
(``extensions/service_landing.py``), and still does not.

Every test runs the orchestrator the app builds over a real backend —
only the team is replaced — and sends what the connection would.
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
from igni_core.backend.service_item_desk import PoolChatDesk
from igni_core.core.config.settings_loader import SettingsLoader
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod
from igni_core.protocol.schemas.envelope import Standing
from igni_core.protocol.schemas.rpc import RpcErrorCode
from tests.host_model_helpers import HostRig


class _Team:
    """A team whose run pauses on a shell call — or, ``working``, runs on."""

    instructions: str | None = None
    db: Any = None

    def __init__(self, *, working: bool = False) -> None:
        self.working = working
        self.ran: list[str] = []

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

    async def acontinue_run(self, **kwargs: Any) -> AsyncIterator[Any]:
        for requirement in kwargs.get("requirements") or []:
            if requirement.confirmation:
                self.ran.append(requirement.tool_execution.tool_args["command"])
        return
        yield


class _RealBackendRig(HostRig):
    """The app's orchestrator and host model, over a real boot backend."""

    def __init__(self, tmp_path: Path, team: _Team) -> None:
        super().__init__(tmp_path)
        backend = BackendServer(
            SettingsLoader.load(project_dir=self.project), project_dir=self.project
        )
        backend._session.main_team = team  # type: ignore[assignment]
        self.backend = backend
        self.session = backend  # type: ignore[assignment]


@pytest.fixture
async def paused(tmp_path: Path):
    """A rig whose boot session has a run paused on one approval."""
    rig = await _RealBackendRig(tmp_path, _Team()).start()
    out = [m async for m in rig.backend.run_message("deploy it")]
    assert any(isinstance(m, msg.RunPaused) for m in out), out
    try:
        yield rig
    finally:
        await rig.stop()


def _waiting(rig: _RealBackendRig) -> list[str]:
    return [
        r.requirement_id
        for m in rig.backend.controllers.hitl.waiting_messages()
        for r in m.requirements
    ]


def _from(standing: Standing, message: msg.Message) -> msg.Message:
    message.mark_from("ws-1", standing)
    return message


def _cancel_run(request_id: str) -> msg.RPCRequest:
    return msg.RPCRequest(id=request_id, method=RpcMethod.CANCEL_RUN.value, args={})


async def _send(rig: _RealBackendRig, message: msg.Message) -> list[msg.Message]:
    """Dispatch ``message`` as the orchestrator does; what came back for it."""
    assert rig.orchestrator is not None
    await rig.orchestrator.dispatch(message)
    return [m for m in rig.windows.sent if m.id == message.id]


def _answer(replies: list[msg.Message]) -> msg.RPCResponse:
    [answer] = [m for m in replies if isinstance(m, msg.RPCResponse)]
    return answer


# ── A program ─────────────────────────────────────────────────────


async def test_a_program_s_cancel_run_is_refused_and_the_approval_survives(paused) -> None:
    before = _waiting(paused)

    answer = _answer(await _send(paused, _from(Standing.PROGRAM, _cancel_run("c-1"))))

    assert answer.code is RpcErrorCode.FORBIDDEN
    assert "waiting on an approval" in answer.error
    assert "approver token" in answer.error
    assert _waiting(paused) == before, "nothing is dropped"


async def test_a_program_s_cancel_message_is_refused_too(paused) -> None:
    before = _waiting(paused)

    replies = await _send(paused, _from(Standing.PROGRAM, msg.Cancel(id="c-2")))

    [error] = [m for m in replies if isinstance(m, msg.Error)]
    assert "approver token" in error.text
    assert any(isinstance(m, msg.StreamEnd) for m in replies)
    assert _waiting(paused) == before


# ── Who may answer may end it ─────────────────────────────────────


@pytest.mark.parametrize("standing", [Standing.APPROVER, Standing.WINDOW])
async def test_an_approver_and_the_window_end_the_waiting_run(paused, standing) -> None:
    answer = _answer(await _send(paused, _from(standing, _cancel_run("c-3"))))

    assert answer.error is None, answer.error
    assert answer.result == {"cancelled": True}
    assert _waiting(paused) == []


# ── A working run stays anybody's to stop ─────────────────────────


@pytest.mark.parametrize("standing", [Standing.PROGRAM, Standing.APPROVER, Standing.WINDOW])
async def test_anybody_who_could_cancel_still_stops_a_working_run(tmp_path: Path, standing) -> None:
    rig = await _RealBackendRig(tmp_path, _Team(working=True)).start()
    try:
        started = asyncio.Event()

        async def run() -> None:
            async for proto in rig.backend.run_message("work"):
                if isinstance(proto, msg.ContentDelta):
                    started.set()

        running = asyncio.create_task(run())
        await asyncio.wait_for(started.wait(), 5)

        answer = _answer(await _send(rig, _from(standing, _cancel_run("c-4"))))

        assert answer.error is None, answer.error
        assert answer.result == {"cancelled": True}
        await asyncio.wait_for(running, 5)
    finally:
        await rig.stop()


async def test_a_program_cancelling_an_idle_session_is_told_nothing_was_cancelled(
    tmp_path: Path,
) -> None:
    rig = await _RealBackendRig(tmp_path, _Team()).start()
    try:
        answer = _answer(await _send(rig, _from(Standing.PROGRAM, _cancel_run("c-5"))))
        assert answer.error is None, answer.error
        assert answer.result == {"cancelled": False}
    finally:
        await rig.stop()


# ── An extension, by its grant ────────────────────────────────────


async def test_an_extension_without_the_approvals_grant_does_not_end_the_waiting_run(
    paused,
) -> None:
    ext = paused.install("acme.reviewer", host=["chat.control"])
    before = _waiting(paused)

    reply = await paused.ask(ext, "call", "chat.cancel")

    assert not reply.ok
    assert reply.error.code == "not_granted"
    assert "chat.approvals" in reply.error.message
    assert _waiting(paused) == before


async def test_an_extension_that_may_answer_approvals_ends_the_waiting_run(paused) -> None:
    ext = paused.install("acme.reviewer", host=["chat.control", "chat.approvals"])

    reply = await paused.ask(ext, "call", "chat.cancel")

    assert reply.ok, reply.error
    assert _waiting(paused) == []


async def test_an_extension_with_chat_control_still_stops_a_working_run(tmp_path: Path) -> None:
    rig = await _RealBackendRig(tmp_path, _Team(working=True)).start()
    try:
        ext = rig.install("acme.reviewer", host=["chat.control"])
        started = asyncio.Event()

        async def run() -> None:
            async for proto in rig.backend.run_message("work"):
                if isinstance(proto, msg.ContentDelta):
                    started.set()

        running = asyncio.create_task(run())
        await asyncio.wait_for(started.wait(), 5)

        reply = await rig.ask(ext, "call", "chat.cancel")

        assert reply.ok, reply.error
        await asyncio.wait_for(running, 5)
    finally:
        await rig.stop()


# ── A service, never ──────────────────────────────────────────────


async def test_a_service_still_never_cancels_a_waiting_run(paused) -> None:
    """The lander cancels nothing the pool desk says waits on an approval
    (``test_an_approval_is_never_answered_and_its_run_never_cancelled``);
    over a real backend paused on one, the desk says it does."""
    assert paused.orchestrator is not None
    desk = PoolChatDesk(paused.orchestrator)

    assert desk.busy(paused.backend.session_id)
    assert desk.waiting_on_approval(paused.backend.session_id)
