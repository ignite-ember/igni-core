"""``SchedulerController`` — a task runs under the session's run, behind the gate.

* ``execute`` refuses with the gate's reason while the gate is closed,
  hands the work to the session's run wrapper, runs it under the task's
  run id and puts that id back afterwards (also when the run raises);
* ``start`` builds a real :class:`SchedulerRunner` from the settings over a
  real task store: a due task runs, completes, and the user callbacks and
  the ``TaskCreated`` / ``TaskCompleted`` hooks fire; ``stop`` forgets it;
* ``list_all`` reads the store.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from igni_core.backend.server_scheduler import SchedulerController
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.config.schemas.orchestration import SchedulerConfig
from igni_core.core.run_context import CURRENT_RUN_ID
from igni_core.core.scheduler.models import ScheduledTask, TaskStatus
from igni_core.core.scheduler.prompt import wrap_scheduled_prompt
from igni_core.core.scheduler.store import TaskStore


class _Team:
    def __init__(self, reply: str = "done", error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.seen_run_ids: list[str] = []
        self._reply = reply
        self._error = error

    async def arun(self, prompt, stream=False, run_id=None):
        self.calls.append({"prompt": prompt, "stream": stream, "run_id": run_id})
        self.seen_run_ids.append(CURRENT_RUN_ID.get())
        if self._error:
            raise self._error
        return self._reply


class _Hooks:
    def __init__(self) -> None:
        self.fired: list[tuple[str, object]] = []

    async def execute(self, *, event, payload):
        self.fired.append((event, payload))


@pytest.fixture
def store(tmp_path):
    task_store = TaskStore(db_path=tmp_path / "state.db")
    with patch("igni_core.backend.server_scheduler.TaskStore", lambda project_dir: task_store):
        yield task_store


def _session(team=None, hooks=None, tmp_path=None):
    return SimpleNamespace(
        main_team=team or _Team(),
        hook_executor=hooks or _Hooks(),
        session_id="sess-1",
        project_dir=tmp_path,
    )


async def test_a_closed_gate_refuses_the_task_without_running_it() -> None:
    gate = RunGate()
    gate.close("group policy is required and no pack was applied")
    team = _Team()
    controller = SchedulerController(_session(team), None, gate=gate)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="group policy is required"):
        await controller.execute("report")

    assert team.calls == []


async def test_the_task_runs_under_the_session_s_run_and_its_run_id() -> None:
    team = _Team(reply="all green")
    handed: list[str] = []

    async def as_run(work):
        handed.append("as_run")
        assert team.calls == [], "the work is handed over, not started"
        return await work()

    controller = SchedulerController(_session(team), None, gate=RunGate(), as_run=as_run)  # type: ignore[arg-type]

    text = await controller.execute("report", run_id="run-7")

    assert text == "all green"
    assert handed == ["as_run"]
    assert team.calls == [
        {"prompt": wrap_scheduled_prompt("report"), "stream": False, "run_id": "run-7"}
    ]
    assert team.seen_run_ids == ["run-7"]
    assert CURRENT_RUN_ID.get() == ""


async def test_without_a_run_id_the_team_is_asked_for_none() -> None:
    team = _Team()
    controller = SchedulerController(_session(team), None, gate=RunGate())  # type: ignore[arg-type]

    await controller.execute("report")

    assert team.calls[0]["run_id"] is None
    assert team.seen_run_ids == [""]


async def test_the_run_id_is_put_back_when_the_run_raises() -> None:
    team = _Team(error=RuntimeError("model down"))
    controller = SchedulerController(_session(team), None, gate=RunGate())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="model down"):
        await controller.execute("report", run_id="run-9")

    assert CURRENT_RUN_ID.get() == ""


async def test_list_all_can_leave_out_the_finished_tasks(store) -> None:
    done = ScheduledTask.new("old", datetime.now())
    waiting = ScheduledTask.new("new", datetime.now())
    await store.add(done)
    await store.add(waiting)
    await store.update_status(done.id, TaskStatus.completed, result="ok")
    controller = SchedulerController(_session(), None, gate=RunGate())  # type: ignore[arg-type]

    assert {t.description for t in await controller.list_all()} == {"old", "new"}
    assert [t.description for t in await controller.list_all(include_done=False)] == ["new"]
    assert controller.runner is None


async def test_a_due_task_runs_through_the_started_runner_and_fires_callbacks_and_hooks(
    store, tmp_path
) -> None:
    team, hooks = _Team(reply="built"), _Hooks()
    settings = SimpleNamespace(scheduler=SchedulerConfig(poll_interval=1, task_timeout=30, max_concurrent=2))
    controller = SchedulerController(_session(team, hooks, tmp_path), settings, gate=RunGate())  # type: ignore[arg-type]
    task = ScheduledTask.new("build the site", datetime.now())
    await store.add(task)
    started: list[tuple[str, str]] = []
    completed: list[tuple[str, str, bool, str]] = []

    runner = controller.start(
        on_task_started=lambda i, d: started.append((i, d)),
        on_task_completed=lambda i, d, ok, detail: completed.append((i, d, ok, detail)),
    )
    try:
        assert runner.is_running and controller.runner is runner
        assert controller.start() is runner, "starting twice is the same runner"
        # The runner is configured from the settings' ``scheduler`` section.
        assert (runner._poll_interval, runner._task_timeout) == (1, 30)
        assert runner._semaphore._value == 2
        for _ in range(100):
            if completed:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)

        assert started == [(task.id, "build the site")]
        assert completed == [(task.id, "build the site", True, "built")]
        assert [c["prompt"] for c in team.calls] == [wrap_scheduled_prompt("build the site")]
        [kept] = await store.get_all(include_done=True)
        assert (kept.status, kept.result) == (TaskStatus.completed, "built")
        assert [(event, p.task_id) for event, p in hooks.fired] == [
            ("TaskCreated", task.id),
            ("TaskCompleted", task.id),
        ]
        assert hooks.fired[0][1].description == "build the site"
        assert hooks.fired[1][1].status == "completed"
        assert hooks.fired[1][1].session_id == "sess-1"
    finally:
        await controller.stop()

    assert controller.runner is None
    assert runner.is_running is False


async def test_a_failing_task_fires_the_completed_hook_with_status_error(store, tmp_path) -> None:
    team, hooks = _Team(error=RuntimeError("boom")), _Hooks()
    settings = SimpleNamespace(scheduler=SchedulerConfig(poll_interval=1))
    controller = SchedulerController(_session(team, hooks, tmp_path), settings, gate=RunGate())  # type: ignore[arg-type]
    task = ScheduledTask.new("flaky", datetime.now())
    await store.add(task)
    completed: list[tuple] = []

    controller.start(on_task_completed=lambda *a: completed.append(a))
    try:
        for _ in range(100):
            if completed:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)

        assert completed == [(task.id, "flaky", False, "boom")]
        done = [p for event, p in hooks.fired if event == "TaskCompleted"]
        assert [p.status for p in done] == ["error"]
    finally:
        await controller.stop()


async def test_stop_without_a_start_does_nothing_and_a_stopped_runner_can_be_replaced(
    store, tmp_path
) -> None:
    settings = SimpleNamespace(scheduler=SchedulerConfig(poll_interval=1))
    controller = SchedulerController(_session(tmp_path=tmp_path), settings, gate=RunGate())  # type: ignore[arg-type]

    await controller.stop()
    first = controller.start()
    await controller.stop()
    second = controller.start()
    try:
        assert second is not first and second.is_running
    finally:
        await controller.stop()
