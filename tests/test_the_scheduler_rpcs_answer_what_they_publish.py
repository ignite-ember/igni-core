"""The scheduler RPCs, called by their wire arguments, over a real backend.

The backend is built the way a boot builds it, with the task store on a
file under the test's folder, and the agent's team a stand-in that waits
as long as the test lets it. Every call goes through the real dispatch
table.

Pinned:

* ``execute_scheduled_task`` answers the agent's text with the run id it
  was given, or a made one (``sched-`` and twelve hex digits); the team
  is asked for that run, unstreamed, with the framed description;
* while it goes the run is the session's: ``get_processing`` says so, a
  second turn waits, and ``cancel_run`` stops it;
* a refusal by the group policy or a failure by the agent is raised;
* ``cancel_scheduled_task`` names only the task it cancels and leaves a
  finished task as it was; ``get_scheduled_tasks`` lists newest first
  and leaves out finished ones when asked;
* ``start_scheduler`` starts the session's scheduler through the push
  bridge; each call declares its duration, correlation and streams.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from igni_core.backend.rpc_binding import Correlation, RpcArgsError, RpcBinding
from igni_core.backend.rpc_handlers.scheduler import (
    ExecuteScheduledTaskArgs,
    ScheduledTaskRunResult,
    SchedulerRpcHandler,
)
from igni_core.backend.rpc_streams import TOOL_PUSHES
from igni_core.backend.run_controller import RunCancelledError
from igni_core.backend.server import BackendServer
from igni_core.core.config.settings_loader import SettingsLoader
from igni_core.core.scheduler.models import ScheduledTask, TaskStatus
from igni_core.core.scheduler.prompt import wrap_scheduled_prompt
from igni_core.core.scheduler.store import TaskStore
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcDuration, RpcMethod, RpcScope
from igni_core.protocol.schemas.enums import PushChannel
from tests.rpc_tables import rpc_table_for


class _Team:
    """The agent: answers when told to, and records what it was asked."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.reply = "all green"
        self.error: Exception | None = None

    async def arun(self, prompt, stream=False, run_id=None):
        self.calls.append({"prompt": prompt, "stream": stream, "run_id": run_id})
        self.started.set()
        await self.release.wait()
        if self.error:
            raise self.error
        return self.reply


@pytest.fixture
def store(tmp_path):
    task_store = TaskStore(db_path=tmp_path / "state.db")
    with patch("igni_core.backend.server_scheduler.TaskStore", lambda project_dir: task_store):
        yield task_store


@pytest.fixture
def team():
    return _Team()


@pytest.fixture
def backend(tmp_path, store, team) -> BackendServer:
    server = BackendServer(SettingsLoader.load(project_dir=tmp_path), project_dir=tmp_path)
    server._session.main_team = team
    return server


@pytest.fixture
def table(backend):
    return rpc_table_for(backend, MagicMock())


async def _ask(table: dict, method: RpcMethod, wire: dict[str, Any] | None = None) -> Any:
    result = table[method](wire or {})
    return await result if hasattr(result, "__await__") else result


# ── running a task now ────────────────────────────────────────────


async def test_a_task_runs_unstreamed_for_the_given_run_and_answers_its_text(table, team):
    team.release.set()

    answer = await _ask(
        table, RpcMethod.EXECUTE_SCHEDULED_TASK, {"description": "report", "run_id": "run-7"}
    )

    assert answer == ScheduledTaskRunResult(run_id="run-7", result="all green")
    assert team.calls == [
        {"prompt": wrap_scheduled_prompt("report"), "stream": False, "run_id": "run-7"}
    ]


@pytest.mark.parametrize("wire", [{"description": "report"}, {"description": "r", "run_id": ""}])
async def test_a_task_without_a_run_id_is_given_a_made_one(table, team, wire):
    team.release.set()

    answer = await _ask(table, RpcMethod.EXECUTE_SCHEDULED_TASK, wire)

    assert re.fullmatch(r"sched-[0-9a-f]{12}", answer.run_id)
    assert team.calls[0]["run_id"] == answer.run_id


async def test_two_runs_without_ids_are_not_given_the_same_one(table, team):
    team.release.set()
    wire = {"description": "report"}

    first = await _ask(table, RpcMethod.EXECUTE_SCHEDULED_TASK, wire)
    second = await _ask(table, RpcMethod.EXECUTE_SCHEDULED_TASK, wire)

    assert first.run_id != second.run_id


async def test_a_task_needs_its_description_and_refuses_other_keys(table):
    with pytest.raises(RpcArgsError, match="'description' is required"):
        table[RpcMethod.EXECUTE_SCHEDULED_TASK]({})
    with pytest.raises(RpcArgsError, match="'task' is not an argument"):
        table[RpcMethod.EXECUTE_SCHEDULED_TASK]({"description": "x", "task": "y"})
    assert ExecuteScheduledTaskArgs.model_fields["run_id"].default == ""


async def test_a_task_is_the_sessions_run_while_it_goes_and_cancel_run_stops_it(table, team):
    running = asyncio.create_task(
        _ask(table, RpcMethod.EXECUTE_SCHEDULED_TASK, {"description": "report", "run_id": "r"})
    )
    await asyncio.wait_for(team.started.wait(), 2)

    assert await _ask(table, RpcMethod.GET_PROCESSING) is True
    cancelled = await _ask(table, RpcMethod.CANCEL_RUN)
    assert cancelled.cancelled is True
    with pytest.raises(RunCancelledError, match="cancelled by user"):
        await asyncio.wait_for(running, 2)
    assert await _ask(table, RpcMethod.GET_PROCESSING) is False


async def test_a_failing_agent_is_raised_not_answered(table, team):
    team.error = RuntimeError("model down")
    team.release.set()

    with pytest.raises(RuntimeError, match="model down"):
        await _ask(table, RpcMethod.EXECUTE_SCHEDULED_TASK, {"description": "report"})


async def test_a_run_the_group_policy_refuses_is_raised_and_never_reaches_the_agent(
    backend, table, team
):
    backend.controllers.loop.scheduler._gate.close("group policy is required and no pack applied")

    with pytest.raises(RuntimeError, match="group policy is required"):
        await _ask(table, RpcMethod.EXECUTE_SCHEDULED_TASK, {"description": "report"})

    assert team.calls == []


# ── cancelling and listing ────────────────────────────────────────


async def test_cancelling_names_only_the_task_it_cancels(store, table):
    mine = ScheduledTask.new("mine", datetime.now())
    other = ScheduledTask.new("other", datetime.now())
    await store.add(mine)
    await store.add(other)

    info = await _ask(table, RpcMethod.CANCEL_SCHEDULED_TASK, {"task_id": mine.id})

    assert isinstance(info, msg.Info)
    assert info.text == f"Cancelled task {mine.id}"
    status = {t.id: t.status for t in await store.get_all(include_done=True)}
    assert status == {mine.id: TaskStatus.cancelled, other.id: TaskStatus.pending}


async def test_an_unknown_or_finished_task_is_refused_and_left_as_it_was(store, table):
    done = ScheduledTask.new("done", datetime.now())
    await store.add(done)
    await store.update_status(done.id, TaskStatus.completed, result="all green")

    with pytest.raises(ValueError, match="no scheduled task 'nope'"):
        await _ask(table, RpcMethod.CANCEL_SCHEDULED_TASK, {"task_id": "nope"})
    with pytest.raises(ValueError, match="already completed"):
        await _ask(table, RpcMethod.CANCEL_SCHEDULED_TASK, {"task_id": done.id})
    with pytest.raises(RpcArgsError, match="'task_id' is required"):
        table[RpcMethod.CANCEL_SCHEDULED_TASK]({})

    [kept] = await store.get_all(include_done=True)
    assert (kept.status, kept.result) == (TaskStatus.completed, "all green")


async def test_the_list_is_newest_first_and_can_leave_out_the_finished(store, table):
    now = datetime.now()
    old = ScheduledTask.new("old", now - timedelta(hours=2))
    new = ScheduledTask.new("new", now)
    done = ScheduledTask.new("done", now - timedelta(hours=1))
    for task in (old, new, done):
        await store.add(task)
    await store.update_status(done.id, TaskStatus.completed, result="ok")

    everything = await _ask(table, RpcMethod.GET_SCHEDULED_TASKS)
    explicit = await _ask(table, RpcMethod.GET_SCHEDULED_TASKS, {"include_done": True})
    open_only = await _ask(table, RpcMethod.GET_SCHEDULED_TASKS, {"include_done": False})

    assert [t.description for t in everything] == ["new", "done", "old"]
    assert [t.description for t in explicit] == ["new", "done", "old"]
    assert [t.description for t in open_only] == ["new", "old"]


async def test_no_tasks_is_an_empty_list(table):
    assert await _ask(table, RpcMethod.GET_SCHEDULED_TASKS, {"include_done": False}) == []


# ── starting the scheduler ────────────────────────────────────────


def test_starting_the_scheduler_hands_this_session_to_the_push_bridge(backend):
    started: list[Any] = []
    push = SimpleNamespace(start_scheduler=started.append)
    calls = SchedulerRpcHandler(SimpleNamespace(backend=backend, push=push)).methods()  # type: ignore[arg-type]

    assert calls[RpcMethod.START_SCHEDULER]({}) is None
    with pytest.raises(RpcArgsError, match="not an argument"):
        calls[RpcMethod.START_SCHEDULER]({"now": True})

    assert started == [backend]


async def test_the_real_bridge_starts_a_scheduler_that_a_second_start_leaves_alone(
    backend, tmp_path
):
    from igni_core.backend.push_bridge import PushNotificationBridge

    loop = asyncio.get_running_loop()
    push = PushNotificationBridge(transport=MagicMock(), loop=loop, queue=[])
    calls = SchedulerRpcHandler(SimpleNamespace(backend=backend, push=push)).methods()  # type: ignore[arg-type]
    scheduler = backend.controllers.loop.scheduler

    try:
        calls[RpcMethod.START_SCHEDULER]({})
        first = scheduler.runner
        calls[RpcMethod.START_SCHEDULER]({})
        assert first is not None
        assert scheduler.runner is first
    finally:
        await scheduler.stop()


# ── what each call declares ───────────────────────────────────────


def test_each_call_declares_its_duration_correlation_and_streams():
    bindings = RpcBinding.on(SchedulerRpcHandler)

    execute = bindings[RpcMethod.EXECUTE_SCHEDULED_TASK][1]
    assert execute.duration is RpcDuration.LONG
    assert execute.correlate == Correlation(by="run_id", source="result.run_id")
    assert execute.may_stream == TOOL_PUSHES and execute.streams == ()
    start = bindings[RpcMethod.START_SCHEDULER][1]
    assert start.correlate == Correlation(by="channel")
    assert start.streams == (PushChannel.SCHEDULER_STARTED, PushChannel.SCHEDULER_COMPLETED)
    assert start.may_stream == TOOL_PUSHES
    for method in (
        RpcMethod.CANCEL_SCHEDULED_TASK,
        RpcMethod.GET_SCHEDULED_TASKS,
        RpcMethod.START_SCHEDULER,
    ):
        assert bindings[method][1].duration is RpcDuration.IO, method
    assert all(b.scope is RpcScope.SESSION for _, b in bindings.values())
