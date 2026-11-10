"""Cancelling and discarding say what they did, and touch only what they name.

* ``cancel_scheduled_task`` answered "Cancelled" for an id nobody had,
  and cancelling a finished task overwrote its status and blanked its
  result and error.
* ``cancel_agent_run`` answered ``ok`` for a run id that was not running.
* ``discard_interrupted_run`` ignored its ``session_id`` — any session's
  row of that id went, interrupted or not — and answered ``ok`` when the
  store failed.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from igni_core.backend.run_controller import RunController
from igni_core.backend.server_scheduler import SchedulerController
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.scheduler.models import ScheduledTask, TaskStatus
from igni_core.core.scheduler.store import TaskStore
from igni_core.core.session.pending_messages import PendingMessageStore


@pytest.fixture
def tasks(tmp_path):
    store = TaskStore(db_path=tmp_path / "state.db")
    with patch("igni_core.backend.server_scheduler.TaskStore", lambda project_dir: store):
        yield (
            store,
            SchedulerController(  # type: ignore[arg-type]
                SimpleNamespace(project_dir=tmp_path), None, gate=RunGate()
            ),
        )


async def test_cancelling_an_unknown_task_is_refused(tasks) -> None:
    _, scheduler = tasks
    with pytest.raises(ValueError, match="no scheduled task 'nope'"):
        await scheduler.cancel("nope")


async def test_cancelling_a_finished_task_leaves_its_record(tasks) -> None:
    store, scheduler = tasks
    task = ScheduledTask.new("report", datetime.now())
    await store.add(task)
    await store.update_status(task.id, TaskStatus.completed, result="all green")

    with pytest.raises(ValueError, match="already completed"):
        await scheduler.cancel(task.id)

    [kept] = await store.get_all(include_done=True)
    assert (kept.status, kept.result) == (TaskStatus.completed, "all green")


async def test_cancelling_a_pending_task_cancels_it(tasks) -> None:
    store, scheduler = tasks
    task = ScheduledTask.new("report", datetime.now())
    await store.add(task)

    info = await scheduler.cancel(task.id)

    assert info.text == f"Cancelled task {task.id}"
    [kept] = await store.get_all(include_done=True)
    assert kept.status is TaskStatus.cancelled


def test_cancelling_a_run_that_is_not_running_is_not_ok() -> None:
    controller = RunController.__new__(RunController)

    result = controller.cancel_agent_run("run-nobody-started")

    assert not result.ok
    assert "is running" in result.error


def test_discarding_takes_only_that_session_s_interrupted_run(tmp_path) -> None:
    store = PendingMessageStore(tmp_path / "state.db")
    mine = store.record_received("s-a", "mine")
    store.mark_interrupted(mine, "cancelled")
    pending = store.record_received("s-a", "still running")
    theirs = store.record_received("s-b", "theirs")
    store.mark_interrupted(theirs, "cancelled")

    assert store.discard_interrupted("s-a", theirs) == 0, "another session's run"
    assert store.discard_interrupted("s-a", pending) == 0, "not an interrupted run"
    assert store.discard_interrupted("s-a", mine) == 1
    assert [m.message_id for m in store.list_interrupted("s-b")] == [theirs]
    assert [m.message_id for m in store.list_pending("s-a")] == [pending]


def test_a_store_that_cannot_be_written_is_told(tmp_path) -> None:
    store = PendingMessageStore(tmp_path / "state.db")
    with (
        patch.object(store._db, "sync_session", side_effect=RuntimeError("disk gone")),
        pytest.raises(RuntimeError, match="disk gone"),
    ):
        store.discard_interrupted("s-a", "m-1")
