"""Only who may answer an approval may write into the session waiting on it.

A new message while a run stands paused ends that run and drops its
approvals (``tests/test_a_new_message_drops_the_paused_run_s_approvals.py``),
as a cancel does. That is refusing them, and a program decides nothing
for the person (``backend/standing_policy.py``): the cancel already takes
an answer's standing (``tests/test_only_an_approver_ends_a_run_waiting_on_an_approval.py``),
and a program without an approver token could still end the pause by
writing into the session instead. So while a session waits on the
person, ``user_message`` takes that standing too: igni's window, or a
program the window made an approver. Refused, nothing is dropped and
nothing is queued. A session that waits on nobody stays anybody's to
write to, as it was.

The extension's ``chat.post`` into the session on screen is held to the
same rule by its grant: while the run waits it needs ``chat.approvals``.

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
from agno.run.agent import RunCompletedEvent, RunContentEvent, RunPausedEvent
from agno.run.requirement import RunRequirement

from igni_core.backend.server import BackendServer
from igni_core.core.config.settings_loader import SettingsLoader
from igni_core.protocol import messages as msg
from igni_core.protocol.schemas.envelope import Standing
from tests.host_model_helpers import HostRig


class _Team:
    """Its first run pauses on a shell call; every later one just answers
    (``runs=1``: none pauses). Continuing a run with the call confirmed is
    the tool running."""

    instructions: str | None = None
    db: Any = None

    def __init__(self, *, runs: int = 0) -> None:
        self.runs = runs
        self.ran: list[str] = []

    async def arun(self, *_: Any, **__: Any) -> AsyncIterator[Any]:
        self.runs += 1
        if self.runs == 1:
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
            return
        yield RunContentEvent(content="Sure.", run_id=f"run-{self.runs}")
        yield RunCompletedEvent(run_id=f"run-{self.runs}")

    async def acontinue_run(self, **kwargs: Any) -> AsyncIterator[Any]:
        for requirement in kwargs.get("requirements") or []:
            if requirement.confirmation:
                self.ran.append(requirement.tool_execution.tool_args["command"])
        yield RunCompletedEvent(run_id=kwargs["run_id"])


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
        self.team = team
        self.session = backend  # type: ignore[assignment]


def _waiting(backend: BackendServer) -> list[str]:
    return [
        r.requirement_id
        for m in backend.controllers.hitl.waiting_messages()
        for r in m.requirements
    ]


async def _send(
    rig: _RealBackendRig, message: msg.Message, standing: Standing
) -> list[msg.Message]:
    """Dispatch ``message`` from a connection of ``standing``; what came back for it."""
    message.mark_from("ws-1", standing)
    assert rig.orchestrator is not None
    await rig.orchestrator.dispatch(message)
    return [m for m in rig.windows.sent if m.id == message.id]


def _echoes(rig: _RealBackendRig, text: str) -> list[msg.UserMessageReceived]:
    return [m for m in rig.windows.of(msg.UserMessageReceived) if m.text == text]


async def _until(predicate: Any) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never happened")


@pytest.fixture
async def paused(tmp_path: Path):
    """A rig whose boot session has a run paused on one approval."""
    rig = await _RealBackendRig(tmp_path, _Team()).start()
    await _send(rig, msg.UserMessage(id="m-1", text="deploy it"), Standing.WINDOW)
    assert len(_waiting(rig.backend)) == 1
    try:
        yield rig
    finally:
        await rig.stop()


# ── A program ─────────────────────────────────────────────────────


async def test_a_program_s_new_message_is_refused_and_the_approval_survives(paused) -> None:
    before = _waiting(paused.backend)

    replies = await _send(paused, msg.UserMessage(id="m-2", text="never mind"), Standing.PROGRAM)

    [error] = [m for m in replies if isinstance(m, msg.Error)]
    assert error.text.startswith("user_message: ")
    assert "waiting on an approval" in error.text
    assert "approver token" in error.text
    assert any(isinstance(m, msg.StreamEnd) for m in replies), "the stream it waits on ends"
    assert _waiting(paused.backend) == before, "nothing is dropped"
    assert paused.team.runs == 1, "no run starts"
    assert _echoes(paused, "never mind") == [], "no window shows it"
    assert paused.orchestrator is not None
    runtime = paused.orchestrator.pool.find(paused.backend.session_id)
    assert runtime is not None and not list(runtime.queue), "nothing is queued"


async def test_the_approval_a_refused_message_left_still_runs_its_tool(paused) -> None:
    (requirement_id,) = _waiting(paused.backend)
    await _send(paused, msg.UserMessage(id="m-2", text="never mind"), Standing.PROGRAM)

    await _send(
        paused,
        msg.HITLResponse(id="h-1", requirement_id=requirement_id, action="confirm"),
        Standing.WINDOW,
    )

    assert paused.team.ran == ["make deploy"], "the person's answer still decides"


# ── Who may answer may write ──────────────────────────────────────


@pytest.mark.parametrize("standing", [Standing.APPROVER, Standing.WINDOW])
async def test_an_approver_and_the_window_write_and_the_approval_goes(paused, standing) -> None:
    replies = await _send(paused, msg.UserMessage(id="m-2", text="status?"), standing)

    assert not [m for m in replies if isinstance(m, msg.Error)]
    assert paused.team.runs == 2, "the new message is answered"
    assert _waiting(paused.backend) == []
    assert paused.team.ran == []


# ── A session waiting on nobody stays anybody's to write to ───────


@pytest.mark.parametrize("standing", [Standing.PROGRAM, Standing.APPROVER, Standing.WINDOW])
async def test_anybody_writes_into_a_session_that_waits_on_nobody(tmp_path: Path, standing) -> None:
    rig = await _RealBackendRig(tmp_path, _Team(runs=1)).start()
    try:
        replies = await _send(rig, msg.UserMessage(id="m-1", text="hello"), standing)

        assert not [m for m in replies if isinstance(m, msg.Error)]
        assert rig.team.runs == 2
        assert _echoes(rig, "hello")
    finally:
        await rig.stop()


# ── After a restart ───────────────────────────────────────────────


async def test_a_program_is_refused_after_a_restart_too(tmp_path: Path) -> None:
    """The approvals a previous backend left are asked again; the session
    waits on the person, so a program still may not write into it."""
    before = await _RealBackendRig(tmp_path, _Team()).start()
    try:
        await _send(before, msg.UserMessage(id="m-1", text="deploy it"), Standing.WINDOW)
        (requirement_id,) = _waiting(before.backend)
        session_id = before.backend.session_id
    finally:
        await before.stop()

    after = await _RealBackendRig(tmp_path, _Team(runs=1), resume=session_id).start()
    try:
        assert _waiting(after.backend) == [requirement_id], "asked again"

        replies = await _send(after, msg.UserMessage(id="m-2", text="moving on"), Standing.PROGRAM)

        [error] = [m for m in replies if isinstance(m, msg.Error)]
        assert "approver token" in error.text
        assert _waiting(after.backend) == [requirement_id]
        assert after.team.runs == 1

        await _send(after, msg.UserMessage(id="m-3", text="moving on"), Standing.APPROVER)

        assert _waiting(after.backend) == [], "an approver still moves on"
        assert after.team.runs == 2
    finally:
        await after.stop()


# ── An extension, by its grant ────────────────────────────────────


async def test_an_extension_without_the_approvals_grant_does_not_post_into_the_waiting_session(
    paused,
) -> None:
    ext = paused.install("acme.reviewer", host=["chat.post"])
    before = _waiting(paused.backend)

    reply = await paused.ask(ext, "call", "chat.post", text="skip that")
    await paused.settle()

    assert not reply.ok
    assert reply.error.code == "not_granted"
    assert "chat.approvals" in reply.error.message
    assert _waiting(paused.backend) == before
    assert paused.team.runs == 1
    assert _echoes(paused, "skip that") == []


async def test_an_extension_that_may_answer_approvals_posts_and_the_approval_goes(
    paused,
) -> None:
    ext = paused.install("acme.reviewer", host=["chat.post", "chat.approvals"])

    reply = await paused.ask(ext, "call", "chat.post", text="skip that")
    await _until(lambda: paused.team.runs == 2)
    await paused.settle()

    assert reply.ok, reply.error
    assert _waiting(paused.backend) == []
    assert paused.team.ran == []


async def test_an_extension_with_chat_post_still_posts_into_a_session_that_waits_on_nobody(
    tmp_path: Path,
) -> None:
    rig = await _RealBackendRig(tmp_path, _Team(runs=1)).start()
    try:
        ext = rig.install("acme.reviewer", host=["chat.post"])

        reply = await rig.ask(ext, "call", "chat.post", text="hello")
        await _until(lambda: rig.team.runs == 2)
        await rig.settle()

        assert reply.ok, reply.error
    finally:
        await rig.stop()
