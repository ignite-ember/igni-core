"""``cancel_run`` stops every run of the session, whatever state it is in.

Session ``6fa3e09e``: a run paused on an approval, the person answered,
and the run went on (``continuing``). A message sent meanwhile started a
second run beside it. The run controller kept one task slot: the second
run took it, and when that run ended it emptied it. The resumed run went
on for minutes after, out of reach — every Escape answered
``cancelled=false`` and ``get_processing`` said the session was idle.

Each test runs a real backend with only the team replaced: a run that
works until stopped, one paused on an approval, one continuing after its
approval, and a turn that waits for a continuing run. Every one ends with ``cancelled=true``, its
stream says it was cancelled, the team's own run is stopped, and the
session is idle again.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from agno.models.response import ToolExecution
from agno.run.agent import RunCompletedEvent, RunContentEvent, RunPausedEvent
from agno.run.requirement import RunRequirement

from igni_core.backend.activity import ActivityState
from igni_core.backend.schemas_run import CancelRunResult
from igni_core.backend.server import BackendServer
from igni_core.core.config.settings_loader import SettingsLoader
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod
from tests.rpc_tables import rpc_table_for

CANCELLED = "Run cancelled by user."


class _Team:
    """A team whose first run pauses on a shell call (or works, ``pauses=False``).

    A continued run says ``continuing`` and then works until stopped; a
    later run says ``second`` and ends, or works until stopped when
    ``second_waits``. ``stopped`` names every run whose stream was
    cancelled.
    """

    instructions: str | None = None
    db: Any = None

    def __init__(self, *, pauses: bool = True, second_waits: bool = False) -> None:
        self._pauses = pauses
        self._second_waits = second_waits
        self._runs = 0
        self.stopped: list[str] = []

    async def aget_session(self, **_: Any) -> None:
        """No stored runs: every stamped turn is one the history leaves out."""
        return None

    async def arun(self, *_: Any, **__: Any) -> AsyncIterator[Any]:
        self._runs += 1
        if self._runs == 1 and self._pauses:
            yield RunPausedEvent(run_id="run-1", requirements=[_shell_call()])
            return
        run_id = f"run-{self._runs}"
        if self._runs == 1 or self._second_waits:
            yield RunContentEvent(content="working", run_id=run_id)
            await self._work(run_id)
        yield RunContentEvent(content="second", run_id=run_id)
        yield RunCompletedEvent(run_id=run_id)

    async def acontinue_run(self, **kwargs: Any) -> AsyncIterator[Any]:
        yield RunContentEvent(content="continuing", run_id=kwargs["run_id"])
        await self._work(kwargs["run_id"])
        yield RunCompletedEvent(run_id=kwargs["run_id"])

    async def _work(self, run_id: str) -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.stopped.append(run_id)
            raise


def _shell_call() -> RunRequirement:
    return RunRequirement(
        tool_execution=ToolExecution(
            tool_call_id="call-1",
            tool_name="run_shell_command",
            tool_args={"command": "printf OK > marker"},
            requires_confirmation=True,
        )
    )


def _backend(project: Path, team: _Team, resume: str | None = None) -> BackendServer:
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


class _Stream:
    """One stream of the session consumed in a task of its own, as the
    dispatcher consumes each message's."""

    def __init__(self, frames: AsyncIterator[msg.Message]) -> None:
        self.got: list[msg.Message] = []
        self.task = asyncio.create_task(self._consume(frames))

    async def _consume(self, frames: AsyncIterator[msg.Message]) -> None:
        async for proto in frames:
            self.got.append(proto)

    def said(self, text: str) -> bool:
        return any(isinstance(m, msg.ContentDelta) and m.text == text for m in self.got)

    def cancelled(self) -> bool:
        return any(isinstance(m, msg.Info) and m.text == CANCELLED for m in self.got)

    async def until(self, ready: Callable[[], bool]) -> None:
        for _ in range(500):
            if ready():
                return
            if self.task.done():
                self.task.result()
                raise AssertionError(f"the stream ended first: {self.got}")
            await asyncio.sleep(0.01)
        raise AssertionError(f"never reached: {self.got}")


async def _pause(backend: BackendServer) -> str:
    out = [m async for m in backend.run_message("run the command")]
    (paused,) = [m for m in out if isinstance(m, msg.RunPaused)]
    return paused.requirements[0].requirement_id


async def _continuing(backend: BackendServer) -> _Stream:
    """Pause, answer, and return the resumed run's stream once it works."""
    requirement_id = await _pause(backend)
    resumed = _Stream(backend.resolve_hitl(requirement_id, "confirm", "once"))
    await resumed.until(lambda: resumed.said("continuing"))
    return resumed


def _idle(backend: BackendServer) -> None:
    assert not backend.processing, "get_processing says the session is idle"
    assert backend.activity().state is ActivityState.IDLE, "every window is told it is idle"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return tmp_path


async def test_cancelling_a_running_run_stops_it(project: Path) -> None:
    team = _Team(pauses=False)
    backend = _backend(project, team)
    running = _Stream(backend.run_message("work"))
    await running.until(lambda: running.said("working"))

    assert await _cancel(backend) == CancelRunResult(cancelled=True)
    await asyncio.wait_for(running.task, 5)

    assert running.cancelled()
    assert team.stopped == ["run-1"]
    _idle(backend)


async def test_cancelling_a_run_paused_on_an_approval_drops_the_approval(project: Path) -> None:
    backend = _backend(project, _Team())
    await _pause(backend)
    assert backend.activity().state is ActivityState.WAITING

    assert await _cancel(backend) == CancelRunResult(cancelled=True)

    assert backend.controllers.hitl.waiting_messages() == []
    _idle(backend)


async def test_cancelling_a_run_continuing_after_its_approval_stops_it(project: Path) -> None:
    team = _Team()
    backend = _backend(project, team)
    resumed = await _continuing(backend)
    assert backend.activity().state is ActivityState.RUNNING

    assert await _cancel(backend) == CancelRunResult(cancelled=True)
    await asyncio.wait_for(resumed.task, 5)

    assert resumed.cancelled()
    assert team.stopped == ["run-1"], "the team's continued run is stopped too"
    _idle(backend)
    restarted = _backend(project, _Team(), resume=backend.session_id)
    assert [m async for m in restarted.controllers.hitl.resume_interrupted()] == [], (
        "no continuing mark is left for a restart to report"
    )


async def test_a_turn_sent_while_a_run_continues_waits_and_the_cancel_reaches_the_run(
    project: Path,
) -> None:
    """The 6fa3e09e order: a message's turn arrives while the resumed run goes
    on. It waits for that run (never beside it, blind to it); the cancel stops
    the resumed run, and the waiting turn runs after it."""
    team = _Team()
    backend = _backend(project, team)
    resumed = await _continuing(backend)
    second = _Stream(backend.run_message("No, search the internet for this one"))
    for _ in range(50):
        await asyncio.sleep(0)
    assert not second.said("second"), "no turn starts beside the resumed run"

    assert backend.processing, "the resumed run still goes on"
    assert backend.activity().state is ActivityState.RUNNING
    assert await _cancel(backend) == CancelRunResult(cancelled=True)
    await asyncio.wait_for(asyncio.gather(resumed.task, second.task), 5)

    assert resumed.cancelled()
    assert team.stopped == ["run-1"]
    assert second.said("second"), "the waiting turn ran once the resumed run let go"
    _idle(backend)


async def test_a_cancel_stops_the_continuing_run_then_the_turn_that_waited_for_it(
    project: Path,
) -> None:
    team = _Team(second_waits=True)
    backend = _backend(project, team)
    resumed = await _continuing(backend)
    second = _Stream(backend.run_message("and this too"))

    assert await _cancel(backend) == CancelRunResult(cancelled=True)
    await asyncio.wait_for(resumed.task, 5)
    await second.until(lambda: second.said("working"))
    assert await _cancel(backend) == CancelRunResult(cancelled=True)
    await asyncio.wait_for(second.task, 5)

    assert resumed.cancelled() and second.cancelled()
    assert team.stopped == ["run-1", "run-2"]
    _idle(backend)


async def test_a_turn_nobody_reads_any_more_is_stopped(project: Path) -> None:
    """The stream to the windows went away: the turn, in a task of its own,
    does not go on alone."""
    team = _Team(pauses=False)
    backend = _backend(project, team)
    frames = backend.controllers.runs.run_message("work", None)
    while not isinstance(proto := await anext(frames), msg.ContentDelta):
        assert proto is not None

    await frames.aclose()

    assert team.stopped == ["run-1"]
    _idle(backend)


async def test_cancelling_once_everything_has_stopped_cancels_nothing(project: Path) -> None:
    backend = _backend(project, _Team())
    resumed = await _continuing(backend)
    await _cancel(backend)
    await asyncio.wait_for(resumed.task, 5)

    assert await _cancel(backend) == CancelRunResult(cancelled=False)
