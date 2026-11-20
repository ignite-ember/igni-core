"""A new message while a run stands paused drops that run's approvals.

The person who writes again instead of answering has moved on: the
paused run's message was closed, but its approvals stayed waiting and
answerable. Answering one then continued the abandoned run and ran its
tool in the middle of the new conversation. A new message now drops
them as a cancel does (``tests/test_cancel_ends_a_paused_run.py``) —
the store's, the journal's, and the sub-agent approvals a previous
backend left — so the old requirement is unknown and runs nothing.

Dropping them refuses them, so only who may answer may send that
message: igni's window, or a program the window made an approver
(``tests/test_only_an_approver_writes_into_a_session_waiting_on_an_approval.py``).

Every test runs the orchestrator the app builds over a real backend —
only the team is replaced — and sends what the connection would.
"""

from __future__ import annotations

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


def _shell_call(command: str) -> RunRequirement:
    return RunRequirement(
        tool_execution=ToolExecution(
            tool_call_id="call-1",
            tool_name="run_shell_command",
            tool_args={"command": command},
            requires_confirmation=True,
        )
    )


class _Team:
    """Its first run pauses on a shell call; every later one just answers.
    Continuing a run with the call confirmed is the tool running."""

    instructions: str | None = None
    db: Any = None

    def __init__(self) -> None:
        self.runs = 0
        self.ran: list[str] = []
        self.continued: list[dict[str, Any]] = []

    async def arun(self, *_: Any, **__: Any) -> AsyncIterator[Any]:
        self.runs += 1
        if self.runs == 1:
            yield RunPausedEvent(run_id="run-1", requirements=[_shell_call("make deploy")])
            return
        yield RunContentEvent(content="Sure.", run_id=f"run-{self.runs}")
        yield RunCompletedEvent(run_id=f"run-{self.runs}")

    async def acontinue_run(self, **kwargs: Any) -> AsyncIterator[Any]:
        self.continued.append(kwargs)
        for requirement in kwargs.get("requirements") or []:
            if requirement.confirmation:
                self.ran.append(requirement.tool_execution.tool_args["command"])
        yield RunCompletedEvent(run_id=kwargs["run_id"])


class _RealBackendRig(HostRig):
    """The app's orchestrator over a real boot backend."""

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


async def _send(rig: _RealBackendRig, message: msg.Message, standing: Standing) -> None:
    message.mark_from("ws-1", standing)
    assert rig.orchestrator is not None
    await rig.orchestrator.dispatch(message)


def _errors_for(rig: _RealBackendRig, request_id: str) -> list[str]:
    return [m.text for m in rig.windows.sent if isinstance(m, msg.Error) and m.id == request_id]


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


async def test_a_new_message_drops_the_paused_run_s_approval(paused) -> None:
    (requirement_id,) = _waiting(paused.backend)

    await _send(paused, msg.UserMessage(id="m-2", text="never mind"), Standing.WINDOW)

    assert paused.team.runs == 2, "the new message is answered"
    assert _waiting(paused.backend) == [], "no window is offered the old approval"
    restarted = BackendServer(
        SettingsLoader.load(project_dir=paused.project),
        project_dir=paused.project,
        resume_session_id=paused.backend.session_id,
    )
    assert requirement_id not in _waiting(restarted), "nor a restart"


async def test_answering_the_old_approval_after_a_new_message_runs_nothing(paused) -> None:
    (requirement_id,) = _waiting(paused.backend)
    await _send(paused, msg.UserMessage(id="m-2", text="never mind"), Standing.WINDOW)

    await _send(
        paused,
        msg.HITLResponse(id="h-1", requirement_id=requirement_id, action="confirm"),
        Standing.WINDOW,
    )

    assert paused.team.ran == [], "the abandoned run's tool never runs"
    assert paused.team.continued == []
    assert _errors_for(paused, "h-1") == [f"Unknown requirement: {requirement_id}"]


async def test_an_approver_s_new_message_is_answered_and_drops_the_approval(paused) -> None:
    (requirement_id,) = _waiting(paused.backend)

    await _send(paused, msg.UserMessage(id="m-2", text="status?"), Standing.APPROVER)

    assert paused.team.runs == 2, "an approver runs sessions, as a program does"
    assert _waiting(paused.backend) == []
    assert paused.team.ran == []


async def test_approvals_a_previous_backend_left_go_with_a_new_message(tmp_path: Path) -> None:
    before = await _RealBackendRig(tmp_path, _Team()).start()
    try:
        await _send(before, msg.UserMessage(id="m-1", text="deploy it"), Standing.WINDOW)
        (main_id,) = _waiting(before.backend)
        sub_id = await before.backend._session.sub_agent_hitl.push_requirement(
            _shell_call("rm -rf build"), run_id="sub-run", agent_path=["architect"]
        )
        session_id = before.backend.session_id
    finally:
        await before.stop()

    team = _Team()
    team.runs = 1  # the restarted backend's next run just answers
    after = await _RealBackendRig(tmp_path, team, resume=session_id).start()
    try:
        assert sorted(_waiting(after.backend)) == sorted([main_id, sub_id]), "asked again"

        await _send(after, msg.UserMessage(id="m-2", text="moving on"), Standing.WINDOW)

        assert _waiting(after.backend) == []
        for request_id, requirement_id in (("h-1", main_id), ("h-2", sub_id)):
            await _send(
                after,
                msg.HITLResponse(id=request_id, requirement_id=requirement_id, action="confirm"),
                Standing.WINDOW,
            )
            assert _errors_for(after, request_id) == [f"Unknown requirement: {requirement_id}"]
        assert team.ran == [] and team.continued == []
    finally:
        await after.stop()
    again = _RealBackendRig(tmp_path, _Team(), resume=session_id)
    assert _waiting(again.backend) == [], "a restart does not ask again"
