"""TaskStore at the store level: what each call returns and what it leaves alone.

The RPC and the runner reach the store through these calls, so the
answers pinned here are what they report to a user: whether a cancel
found its task and what the task was, whether a status write landed,
which tasks are due and in what order, when the runner next wakes.
"""

import sqlite3
from datetime import datetime, timedelta

import pytest

from igni_core.core.activity import ActivityRegistry
from igni_core.core.db.database import Database
from igni_core.core.scheduler.db_models import ScheduledTaskModel
from igni_core.core.scheduler.models import ScheduledTask, TaskStatus
from igni_core.core.scheduler.store import TaskStore
from tests.storage_backends import Scopes


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "state.db")


@pytest.fixture
def store(db) -> TaskStore:
    return TaskStore(db=db)


def _task(task_id: str = "t1", *, minutes: int = 60, **kwargs) -> ScheduledTask:
    return ScheduledTask(
        id=task_id,
        description=kwargs.pop("description", "do it"),
        scheduled_at=datetime.now() + timedelta(minutes=minutes),
        **kwargs,
    )


async def _insert_raw(db: Database, task_id: str, status: str) -> None:
    async with db.session() as session, session.begin():
        session.add(
            ScheduledTaskModel(
                id=task_id,
                description="raw",
                scheduled_at=datetime.now() - timedelta(minutes=1),
                created_at=datetime.now(),
                status=status,
                result="kept",
                error="",
                recurrence="",
            )
        )


class TestAdd:
    @pytest.mark.asyncio
    async def test_wakes_a_sleeping_runner_after_the_row_is_committed(self, tmp_path, store):
        """A runner asleep until the old earliest task must look again, and
        when it does the new task has to be readable."""
        seen: list[int] = []

        def timer() -> None:
            with sqlite3.connect(tmp_path / "state.db") as conn:
                seen.append(conn.execute("SELECT count(*) FROM scheduler_tasks").fetchone()[0])

        registry = ActivityRegistry.shared()
        registry.on_rearm(timer)
        try:
            await store.add(_task())
        finally:
            registry.off_rearm(timer)

        assert seen == [1]

    @pytest.mark.asyncio
    async def test_a_duplicate_id_is_refused_and_the_first_task_stands(self, store):
        await store.add(_task(description="first"))
        with pytest.raises(Exception):  # noqa: B017, PT011 - the driver's integrity error
            await store.add(_task(description="second"))
        assert (await store.get("t1")).description == "first"

    @pytest.mark.asyncio
    async def test_unicode_and_long_text_are_stored_as_written(self, store):
        text = "проверь ☃ " + "x" * 20000
        await store.add(_task(description=text))
        await store.update_status("t1", TaskStatus.failed, result=text, error=text)
        task = await store.get("t1")
        assert (task.description, task.result, task.error) == (text, text, text)


class TestUpdateStatus:
    @pytest.mark.asyncio
    async def test_an_unknown_id_is_not_written(self, store):
        assert await store.update_status("nope", TaskStatus.completed) is False

    @pytest.mark.asyncio
    async def test_writes_status_result_and_error_and_clears_them(self, store):
        await store.add(_task())

        assert await store.update_status("t1", TaskStatus.failed, "partial", "boom") is True
        task = await store.get("t1")
        assert (task.status, task.result, task.error) == (TaskStatus.failed, "partial", "boom")

        assert await store.update_status("t1", TaskStatus.pending) is True
        task = await store.get("t1")
        assert (task.status, task.result, task.error) == (TaskStatus.pending, "", "")

    @pytest.mark.asyncio
    async def test_unless_cancelled_still_writes_over_an_uncancelled_task(self, store):
        await store.add(_task())
        assert await store.update_status("t1", TaskStatus.running, unless_cancelled=True) is True
        assert (await store.get("t1")).status == TaskStatus.running

    @pytest.mark.asyncio
    async def test_a_cancelled_task_keeps_its_result_against_a_guarded_write(self, store):
        await store.add(_task())
        await store.update_status("t1", TaskStatus.cancelled, "stopped", "by user")

        wrote = await store.update_status(
            "t1", TaskStatus.completed, "late", "", unless_cancelled=True
        )

        task = await store.get("t1")
        assert wrote is False
        assert (task.status, task.result, task.error) == (
            TaskStatus.cancelled,
            "stopped",
            "by user",
        )

    @pytest.mark.asyncio
    async def test_an_unguarded_write_still_overwrites_a_cancel(self, store):
        await store.add(_task())
        await store.update_status("t1", TaskStatus.cancelled)
        assert await store.update_status("t1", TaskStatus.completed) is True
        assert (await store.get("t1")).status == TaskStatus.completed


class TestCancel:
    @pytest.mark.asyncio
    async def test_an_unknown_id_gives_none(self, store):
        assert await store.cancel("nope") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("before", [TaskStatus.pending, TaskStatus.running])
    async def test_an_active_task_becomes_cancelled_and_reports_what_it_was(self, store, before):
        await store.add(_task())
        await store.update_status("t1", before)

        assert await store.cancel("t1") == before
        assert (await store.get("t1")).status == TaskStatus.cancelled

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "before", [TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled]
    )
    async def test_a_finished_task_keeps_status_result_and_error(self, store, before):
        await store.add(_task())
        await store.update_status("t1", before, "the result", "the error")

        assert await store.cancel("t1") == before

        task = await store.get("t1")
        assert (task.status, task.result, task.error) == (before, "the result", "the error")

    @pytest.mark.asyncio
    async def test_a_row_this_build_cannot_read_is_not_found_and_untouched(self, db, store):
        await _insert_raw(db, "odd", "wat")

        assert await store.cancel("odd") is None

        async with db.session() as session:
            row = await session.get(ScheduledTaskModel, "odd")
        assert (row.status, row.result) == ("wat", "kept")


class TestDueAndNext:
    @pytest.mark.asyncio
    async def test_due_tasks_are_pending_only_and_oldest_first(self, store):
        await store.add(_task("late", minutes=-1))
        await store.add(_task("early", minutes=-30))
        await store.add(_task("middle", minutes=-10))
        await store.add(_task("future", minutes=30))
        for task_id, status in (
            ("done", TaskStatus.completed),
            ("busy", TaskStatus.running),
            ("gone", TaskStatus.cancelled),
            ("bad", TaskStatus.failed),
        ):
            await store.add(_task(task_id, minutes=-45))
            await store.update_status(task_id, status)

        assert [t.id for t in await store.get_due_tasks()] == ["early", "middle", "late"]

    @pytest.mark.asyncio
    async def test_a_task_due_this_instant_is_due(self, store, monkeypatch):
        moment = datetime(2030, 1, 1, 12, 0, 0)

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment

        monkeypatch.setattr("igni_core.core.scheduler.store.datetime", Frozen)
        await store.add(
            ScheduledTask(id="edge", description="x", scheduled_at=moment, created_at=moment)
        )
        await store.add(
            ScheduledTask(
                id="after",
                description="x",
                scheduled_at=moment + timedelta(seconds=1),
                created_at=moment,
            )
        )

        assert [t.id for t in await store.get_due_tasks()] == ["edge"]

    @pytest.mark.asyncio
    async def test_an_empty_store_has_nothing(self, store):
        assert await store.get_due_tasks() == []
        assert await store.get_all() == []
        assert await store.next_pending_at() is None
        assert await store.remove("nope") is False

    @pytest.mark.asyncio
    async def test_next_pending_at_is_the_earliest_pending_one(self, store):
        far = _task("far", minutes=120)
        near = _task("near", minutes=10)
        earlier_but_done = _task("done", minutes=1)
        for task in (far, near, earlier_but_done):
            await store.add(task)
        await store.update_status("done", TaskStatus.completed)
        await store.update_status("far", TaskStatus.running)

        assert await store.next_pending_at() == near.scheduled_at

    @pytest.mark.asyncio
    async def test_next_pending_at_is_none_when_nothing_is_pending(self, store):
        await store.add(_task())
        await store.update_status("t1", TaskStatus.completed)
        assert await store.next_pending_at() is None


class TestScope:
    @pytest.mark.asyncio
    async def test_another_scope_cannot_write_cancel_or_see_the_task(self, tmp_path):
        mine = Database(tmp_path / "shared.db", scope=Scopes.fresh())
        theirs = mine.in_scope(Scopes.fresh())
        await TaskStore(db=mine).add(_task(minutes=-5))
        other = TaskStore(db=theirs)

        assert await other.update_status("t1", TaskStatus.failed, "x", "y") is False
        assert await other.cancel("t1") is None
        assert await other.get_due_tasks() == []
        assert await other.next_pending_at() is None

        task = await TaskStore(db=mine).get("t1")
        assert (task.status, task.result, task.error) == (TaskStatus.pending, "", "")
        assert await TaskStore(db=mine).next_pending_at() is not None

    @pytest.mark.asyncio
    async def test_add_stamps_the_scope_it_writes_in(self, tmp_path):
        scope = Scopes.fresh()
        db = Database(tmp_path / "s.db", scope=scope)
        await TaskStore(db=db).add(_task())

        async with db.session() as session:
            row = await session.get(ScheduledTaskModel, "t1")
        assert {c: getattr(row, c) for c in scope.stamp()} == scope.stamp()
