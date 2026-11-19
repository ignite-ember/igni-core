"""Cancelling a run paused on an approval ends it: the approval goes.

A run that paused for the person has no task left — its stream ended
with the ``run_paused`` card — so ``cancel_run`` found nothing to
cancel. It stamped the message cancelled and answered
``cancelled=false``, and the approval stayed: the next window that
attached was offered it again, and answering it ran the tool the person
had just stopped (campaign retest, three times on ``aaf974e1``).

Every test here runs a real backend — the boot ``BackendApp`` does, the
RPC table the window's calls land on — with only the team replaced, so
the pause is a real one: the multiplexer, the pause handler, the
approval journal in the project's database. What we pin:

* cancelling a paused run answers ``cancelled=true``;
* its approval is gone — from the window that attaches, from the
  session list, and from a backend that restarts on the same project;
* answering the old requirement afterwards runs nothing;
* an approval a previous backend left (the main agent's or a
  sub-agent's) is dropped by the cancel too, and answering it continues
  nothing;
* cancelling while a sub-agent waits on an approval ends the run, and
  its question goes with it;
* cancelling an idle session still answers ``cancelled=false``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from agno.models.response import ToolExecution
from agno.run.agent import RunCompletedEvent, RunContentEvent, RunPausedEvent
from agno.run.requirement import RunRequirement

from igni_core.backend.schemas_run import CancelRunResult
from igni_core.backend.server import BackendServer
from igni_core.core.config.settings_loader import SettingsLoader
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod
from tests.rpc_tables import rpc_table_for


class _PausingTeam:
    """A team whose run pauses on one shell call; continuing it runs the call.

    ``ran`` is what the tool did: a continued run with the call confirmed
    is the tool running.
    """

    #: Read by the run's pre-steps (the learning context); none here.
    instructions: str | None = None
    db: Any = None

    def __init__(self) -> None:
        self.ran: list[str] = []
        self.continued: list[dict[str, Any]] = []

    async def arun(self, *_: Any, **__: Any) -> AsyncIterator[Any]:
        yield RunPausedEvent(run_id="run-1", requirements=[_shell_call("printf OK > marker")])

    async def acontinue_run(self, **kwargs: Any) -> AsyncIterator[Any]:
        self.continued.append(kwargs)
        for requirement in kwargs.get("requirements") or []:
            if requirement.confirmation:
                self.ran.append(requirement.tool_execution.tool_args["command"])
        yield RunContentEvent(content="DONE", run_id=kwargs["run_id"])
        yield RunCompletedEvent(run_id=kwargs["run_id"])


def _shell_call(command: str) -> RunRequirement:
    return RunRequirement(
        tool_execution=ToolExecution(
            tool_call_id="call-1",
            tool_name="run_shell_command",
            tool_args={"command": command},
            requires_confirmation=True,
        )
    )


def _backend(project: Path, team: _PausingTeam, resume: str | None = None) -> BackendServer:
    """A backend booted as ``BackendApp`` boots one, running ``team``.

    ``resume`` is the session a restarted backend opens again: it reads
    back what that session left waiting on the person.
    """
    backend = BackendServer(
        SettingsLoader.load(project_dir=project), project_dir=project, resume_session_id=resume
    )
    backend._session.main_team = team  # type: ignore[assignment]
    return backend


async def _cancel(backend: BackendServer) -> CancelRunResult:
    """``cancel_run`` as the window sends it, through the RPC table."""
    answer = rpc_table_for(backend, MagicMock())[RpcMethod.CANCEL_RUN]({})
    if hasattr(answer, "__await__"):
        answer = await answer
    assert isinstance(answer, CancelRunResult)
    return answer


async def _pause(backend: BackendServer) -> str:
    """Send a message whose run pauses; return the approval's requirement id."""
    out = [m async for m in backend.run_message("run the command")]
    paused = [m for m in out if isinstance(m, msg.RunPaused)]
    assert len(paused) == 1, out
    return paused[0].requirements[0].requirement_id


async def _answer(backend: BackendServer, requirement_id: str) -> list[msg.Message]:
    """Approve ``requirement_id`` once, as the window's dialog does."""
    return [m async for m in backend.resolve_hitl(requirement_id, "confirm", "once")]


def _waiting_ids(backend: BackendServer) -> list[str]:
    return [
        r.requirement_id
        for paused in backend.controllers.hitl.waiting_messages()
        for r in paused.requirements
    ]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return tmp_path


async def test_cancelling_a_paused_run_answers_that_it_cancelled(project: Path) -> None:
    backend = _backend(project, _PausingTeam())
    await _pause(backend)

    assert await _cancel(backend) == CancelRunResult(cancelled=True)


async def test_the_cancelled_run_s_approval_is_offered_to_nobody(project: Path) -> None:
    backend = _backend(project, _PausingTeam())
    requirement_id = await _pause(backend)
    assert _waiting_ids(backend) == [requirement_id]

    await _cancel(backend)

    assert _waiting_ids(backend) == [], "a window that attaches is shown no card"
    listed = await backend.controllers.sessions.list_sessions()
    assert [e.has_paused_run for e in listed.sessions if e.session_id == backend.session_id] in (
        [],
        [False],
    ), "the session list does not say the session waits on the person"
    restarted = _backend(project, _PausingTeam(), resume=backend.session_id)
    assert _waiting_ids(restarted) == [], "a restart does not ask again"


async def test_answering_the_old_approval_runs_nothing(project: Path) -> None:
    team = _PausingTeam()
    backend = _backend(project, team)
    requirement_id = await _pause(backend)
    await _cancel(backend)

    replies = await _answer(backend, requirement_id)

    assert team.ran == [], "the tool the person stopped never runs"
    assert team.continued == [], "the cancelled run is not continued"
    errors = [m.text for m in replies if isinstance(m, msg.Error)]
    assert errors == [f"Unknown requirement: {requirement_id}"]


async def test_an_approval_a_previous_backend_left_is_dropped_by_the_cancel(
    project: Path,
) -> None:
    before = _backend(project, _PausingTeam())
    requirement_id = await _pause(before)

    team = _PausingTeam()
    after = _backend(project, team, resume=before.session_id)
    assert _waiting_ids(after) == [requirement_id], "the restart asks again"

    assert await _cancel(after) == CancelRunResult(cancelled=True)
    assert _waiting_ids(after) == []
    await _answer(after, requirement_id)
    assert team.ran == []
    assert _waiting_ids(_backend(project, _PausingTeam(), resume=before.session_id)) == []


async def test_a_sub_agent_s_approval_a_previous_backend_left_is_dropped_too(
    project: Path,
) -> None:
    before = _backend(project, _PausingTeam())
    coordinator = before._session.sub_agent_hitl
    requirement_id = await coordinator.push_requirement(
        _shell_call("rm -rf build"), run_id="sub-run", agent_path=["architect"]
    )

    team = _PausingTeam()
    after = _backend(project, team, resume=before.session_id)
    assert _waiting_ids(after) == [requirement_id], "the restart asks again"

    assert await _cancel(after) == CancelRunResult(cancelled=True)
    assert _waiting_ids(after) == []
    replies = await _answer(after, requirement_id)
    assert [m.text for m in replies if isinstance(m, msg.Error)] == [
        f"Unknown requirement: {requirement_id}"
    ], "answering it continues nothing"
    restarted = _backend(project, _PausingTeam(), resume=before.session_id)
    assert _waiting_ids(restarted) == [], "a restart does not ask again"


async def test_cancelling_an_idle_session_still_answers_that_nothing_was_cancelled(
    project: Path,
) -> None:
    backend = _backend(project, _PausingTeam())

    assert await _cancel(backend) == CancelRunResult(cancelled=False)


async def test_cancelling_a_second_time_answers_that_nothing_was_cancelled(
    project: Path,
) -> None:
    backend = _backend(project, _PausingTeam())
    await _pause(backend)
    await _cancel(backend)

    assert await _cancel(backend) == CancelRunResult(cancelled=False)


class _TeamWhoseSubAgentPauses(_PausingTeam):
    """A team whose run is a spawn waiting on its sub-agent's approval.

    It waits as ``spawn_agent``'s stream handler does
    (``orchestrate_agent_stream``): push the question, wait for the
    answer, drop the question however the wait ends.
    """

    def __init__(self, coordinator: Any) -> None:
        super().__init__()
        self._coordinator = coordinator

    async def arun(self, *_: Any, **__: Any) -> AsyncIterator[Any]:
        requirement_id = await self._coordinator.push_requirement(
            _shell_call("rm -rf build"), run_id="sub-run", agent_path=["architect"]
        )
        try:
            requirement = await self._coordinator.wait_resolved(requirement_id)
        finally:
            self._coordinator.cleanup(requirement_id)
        if requirement.confirmation:
            self.ran.append("rm -rf build")
        yield RunContentEvent(content="DONE", run_id="run-1")


async def test_cancelling_while_a_sub_agent_waits_ends_the_run_and_its_question(
    project: Path,
) -> None:
    backend = _backend(project, _PausingTeam())
    coordinator = backend._session.sub_agent_hitl
    team = _TeamWhoseSubAgentPauses(coordinator)
    backend._session.main_team = team  # type: ignore[assignment]
    out: list[msg.Message] = []

    async def run() -> None:
        async for proto in backend.run_message("delegate it"):
            out.append(proto)

    running = asyncio.create_task(run())
    for _ in range(200):
        if _waiting_ids(backend):
            break
        await asyncio.sleep(0.01)
    (requirement_id,) = _waiting_ids(backend)

    assert await _cancel(backend) == CancelRunResult(cancelled=True)
    await asyncio.wait_for(running, 5)

    assert any(isinstance(m, msg.Info) and m.text == "Run cancelled by user." for m in out)
    assert _waiting_ids(backend) == []
    await _answer(backend, requirement_id)
    assert team.ran == []
    restarted = _backend(project, _PausingTeam(), resume=backend.session_id)
    assert _waiting_ids(restarted) == [], "a restart does not ask again"
