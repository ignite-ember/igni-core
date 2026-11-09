"""Scheduler: missed-occurrence catch-up, cancel-during-execution, unreadable rows.

Covers SWP-38 (every occurrence missed while igni was closed was
replayed as a full unattended agent run), SWP-39 (a cancel issued while
the task was running was overwritten and the next occurrence created
anyway) and SWP-55 (a row the domain cannot read raised
``ValidationError`` out of ``get_due_tasks`` and killed the poll loop).

The persisted shape is ours alone: the ``recurrence`` column holds the
canonical form, and a row holding anything else is data this build does
not recognise. Such a row is read as one-shot and the rest of the
project keeps running.
"""

from datetime import datetime, timedelta

import pytest

from igni_core.core.db.database import Database
from igni_core.core.scheduler.db_models import ScheduledTaskModel
from igni_core.core.scheduler.models import ScheduledTask, TaskStatus
from igni_core.core.scheduler.recurrence import Recurrence
from igni_core.core.scheduler.runner import SchedulerRunner
from igni_core.core.scheduler.store import TaskStore


@pytest.fixture
def db(tmp_path) -> Database:
    """The project's database — the store and the raw-row helper share it."""
    return Database(tmp_path / "state.db")


@pytest.fixture
def store(db) -> TaskStore:
    return TaskStore(db=db)


# ── SWP-38: missed occurrences collapse ─────────────────────────


class TestCatchUp:
    def test_next_after_without_not_before_is_unchanged(self):
        r = Recurrence.from_canonical("every 1 hours")
        anchor = datetime(2026, 3, 19, 14, 0)
        assert r.next_after(anchor) == datetime(2026, 3, 19, 15, 0)

    def test_missed_occurrences_collapse_to_the_next_future_one(self):
        """48h offline with an hourly task used to yield an anchor that was
        itself in the past — the store handed it straight back as due, once
        per missed hour. Now it lands strictly in the future, on phase."""
        r = Recurrence.from_canonical("every 1 hours")
        anchor = datetime(2026, 3, 19, 14, 0)
        now = anchor + timedelta(hours=48, minutes=10)

        nxt = r.next_after(anchor, not_before=now)

        assert nxt > now
        assert nxt == datetime(2026, 3, 21, 15, 0)  # same :00 phase as the anchor
        assert (nxt - anchor) % r.to_delta() == timedelta(0)

    def test_boundary_occurrence_is_not_reused(self):
        """An occurrence exactly at ``not_before`` is already due — return
        the following one rather than something the store re-fires at once."""
        r = Recurrence.from_canonical("every 30 minutes")
        anchor = datetime(2026, 3, 19, 14, 0)
        nxt = r.next_after(anchor, not_before=datetime(2026, 3, 19, 14, 30))
        assert nxt == datetime(2026, 3, 19, 15, 0)

    @pytest.mark.asyncio
    async def test_runner_reschedules_into_the_future(self, store):
        """End to end: a week-old hourly task reschedules once, ahead of now —
        not 168 immediately-due replays."""
        task = ScheduledTask.new(
            description="hourly chore",
            scheduled_at=datetime.now() - timedelta(days=7),
            recurrence="every 1 hours",
        )
        await store.add(task)
        await store.update_status(task.id, TaskStatus.running)

        runner = SchedulerRunner(store=store, execute_fn=_ok, task_timeout=5)
        await runner._execute_task(task.id, task.description)

        pending = [t for t in await store.get_all() if t.id != task.id]
        assert len(pending) == 1
        assert pending[0].scheduled_at > datetime.now()
        assert not await store.get_due_tasks()


# ── SWP-39: cancel during execution ─────────────────────────────


class TestCancelDuringExecution:
    @pytest.mark.asyncio
    async def test_store_write_loses_to_a_cancel(self, store):
        task = ScheduledTask.new(description="x", scheduled_at=datetime.now())
        await store.add(task)
        await store.update_status(task.id, TaskStatus.cancelled)

        wrote = await store.update_status(
            task.id, TaskStatus.completed, result="done", unless_cancelled=True
        )

        assert wrote is False
        assert (await store.get(task.id)).status == TaskStatus.cancelled

    @pytest.mark.asyncio
    async def test_cancel_mid_run_stops_the_recurrence(self, store):
        """The cancel lands while ``execute_fn`` is still running — the window
        the runner keeps open for up to ``task_timeout`` (300s by default)."""
        task = ScheduledTask.new(
            description="misbehaving",
            scheduled_at=datetime.now(),
            recurrence="every 1 hours",
        )
        await store.add(task)
        await store.update_status(task.id, TaskStatus.running)

        async def cancel_midway(_description: str) -> str:
            await store.update_status(task.id, TaskStatus.cancelled)
            return "result the user no longer wants"

        completions: list[tuple] = []
        runner = SchedulerRunner(
            store=store,
            execute_fn=cancel_midway,
            on_task_completed=lambda *a: completions.append(a),
            task_timeout=5,
        )
        await runner._execute_task(task.id, task.description)

        assert (await store.get(task.id)).status == TaskStatus.cancelled
        assert [t.id for t in await store.get_all(include_done=True)] == [task.id]
        # No "completed" push for a run the user cancelled.
        assert completions == []

    @pytest.mark.asyncio
    async def test_uncancelled_recurring_task_still_reschedules(self, store):
        task = ScheduledTask.new(
            description="fine",
            scheduled_at=datetime.now(),
            recurrence="every 1 hours",
        )
        await store.add(task)
        await store.update_status(task.id, TaskStatus.running)

        runner = SchedulerRunner(store=store, execute_fn=_ok, task_timeout=5)
        await runner._execute_task(task.id, task.description)

        assert (await store.get(task.id)).status == TaskStatus.completed
        assert len(await store.get_all(include_done=True)) == 2


# ── SWP-55: rows this build cannot read ─────────────────────────


class TestUnreadableRows:
    @pytest.mark.asyncio
    async def test_unrecognised_recurrence_does_not_break_polling(self, db, store):
        """The ``recurrence`` column holds the canonical form; a row holding
        anything else is data this build does not recognise. Reading it
        must not raise out of ``get_due_tasks`` — that once stopped every
        task in the project from running. The task itself still runs; it
        just does not repeat."""
        await _insert_raw(db, id_="odd", recurrence="daily")

        due = await store.get_due_tasks()

        assert [t.id for t in due] == ["odd"]
        assert due[0].recurrence == ""
        assert due[0].recurrence_obj is None

    @pytest.mark.asyncio
    async def test_unrecognised_recurrence_row_reads_as_one_shot(self, db, store):
        await _insert_raw(db, id_="odd2", recurrence="on a blue moon")
        task = await store.get("odd2")
        assert task is not None
        assert task.recurrence == ""

    @pytest.mark.asyncio
    async def test_canonical_recurrence_round_trips(self, db, store):
        await _insert_raw(db, id_="canon", recurrence="every 7 days")
        task = await store.get("canon")
        assert task is not None
        assert task.recurrence == "every 7 days"
        assert task.recurrence_obj is not None

    @pytest.mark.asyncio
    async def test_unreadable_row_costs_only_itself(self, db, store):
        """A row whose status the domain refuses is left out; the tasks
        around it keep running."""
        await _insert_raw(db, id_="broken", recurrence="", status="wat")
        await _insert_raw(db, id_="fine", recurrence="")

        every = await store.get_all(include_done=True)

        assert [t.id for t in every] == ["fine"]
        assert await store.get("broken") is None

    @pytest.mark.asyncio
    async def test_absurd_recurrence_amount_does_not_break_polling(self, db, store):
        """A canonical-shaped recurrence whose amount cannot be turned into
        an ``int`` (thousands of digits) used to raise ``ValueError`` out of
        the row read. The exception reached the runner's poll loop, where
        the row stays pending and due: every later poll aborted the same
        way, and no task in the project ever ran again."""
        await _insert_raw(db, id_="absurd", recurrence=f"every {'9' * 5000} days")
        await _insert_raw(db, id_="neighbour", recurrence="")

        due = await store.get_due_tasks()

        assert sorted(t.id for t in due) == ["absurd", "neighbour"]
        absurd = next(t for t in due if t.id == "absurd")
        assert absurd.recurrence == ""
        assert absurd.recurrence_obj is None

    @pytest.mark.asyncio
    async def test_absurd_recurrence_row_reads_as_one_shot(self, db, store):
        await _insert_raw(db, id_="absurd2", recurrence=f"every {'9' * 5000} days")
        task = await store.get("absurd2")
        assert task is not None
        assert task.recurrence == ""

    def test_docstring_examples_construct(self):
        """The model docstring names the canonical form; every example in it
        must survive its own validator."""
        for pattern in ("every 1 hours", "every 1 days", "every 7 days"):
            assert ScheduledTask.new("x", datetime.now(), recurrence=pattern)


async def _ok(_description: str) -> str:
    return "ok"


async def _insert_raw(
    db: Database, *, id_: str, recurrence: str, status: str = TaskStatus.pending.value
) -> None:
    """Write a row straight through SQLAlchemy, bypassing the pydantic
    validator — that is exactly how a row the domain refuses gets there."""
    async with db.session() as session, session.begin():
        session.add(
            ScheduledTaskModel(
                id=id_,
                description="raw task",
                scheduled_at=datetime.now() - timedelta(minutes=1),
                created_at=datetime.now(),
                status=status,
                result="",
                error="",
                recurrence=recurrence,
            )
        )
