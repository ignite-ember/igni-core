"""SchedulerRunner against a real store: outcomes, bounds, wake-ups, shutdown."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.core.activity import ActivityRegistry
from igni_core.core.db.database import Database
from igni_core.core.scheduler.models import ScheduledTask, TaskStatus
from igni_core.core.scheduler.runner import SchedulerRunner
from igni_core.core.scheduler.store import TaskStore


@pytest.fixture
def store(tmp_path) -> TaskStore:
    return TaskStore(db=Database(tmp_path / "state.db"))


@pytest.fixture(autouse=True)
def _fresh_registry():
    ActivityRegistry.reset_shared()
    yield
    ActivityRegistry.reset_shared()


async def _running_task(store, *, recurrence="", description="work") -> ScheduledTask:
    task = ScheduledTask.new(
        description=description, scheduled_at=datetime.now(), recurrence=recurrence
    )
    await store.add(task)
    await store.update_status(task.id, TaskStatus.running)
    return task


async def _ok(_description: str) -> str:
    return "ok"


class TestOutcomes:
    async def test_a_result_is_stored_as_completed(self, store):
        task = await _running_task(store)
        runner = SchedulerRunner(store=store, execute_fn=_ok, task_timeout=5)

        await runner._execute_task(task.id, task.description)

        done = await store.get(task.id)
        assert (done.status, done.result, done.error) == (TaskStatus.completed, "ok", "")

    async def test_a_timeout_is_stored_as_failed_and_reported_with_its_reason(self, store):
        task = await _running_task(store)
        completions: list[tuple] = []

        async def forever(_d: str) -> str:
            await asyncio.sleep(30)
            return "never"

        runner = SchedulerRunner(
            store=store,
            execute_fn=forever,
            on_task_completed=lambda *a: completions.append(a),
            task_timeout=0.2,
        )
        await runner._execute_task(task.id, task.description)

        failed = await store.get(task.id)
        assert failed.status == TaskStatus.failed
        assert failed.error == "Task timed out after 0s"
        assert completions == [(task.id, "work", False, "Task timed out after 0s")]

    async def test_an_exception_is_stored_as_failed_with_its_message(self, store):
        task = await _running_task(store)
        completions: list[tuple] = []

        async def boom(_d: str) -> str:
            raise RuntimeError("the disk is full")

        runner = SchedulerRunner(
            store=store,
            execute_fn=boom,
            on_task_completed=lambda *a: completions.append(a),
            task_timeout=5,
        )
        await runner._execute_task(task.id, task.description)

        failed = await store.get(task.id)
        assert (failed.status, failed.error) == (TaskStatus.failed, "the disk is full")
        assert completions == [(task.id, "work", False, "the disk is full")]

    async def test_a_recurring_task_that_failed_still_gets_its_next_occurrence(self, store):
        task = await _running_task(store, recurrence="every 1 hours")

        async def boom(_d: str) -> str:
            raise RuntimeError("nope")

        runner = SchedulerRunner(store=store, execute_fn=boom, task_timeout=5)
        await runner._execute_task(task.id, task.description)

        pending = [t for t in await store.get_all() if t.id != task.id]
        assert len(pending) == 1
        assert pending[0].recurrence == "every 1 hours"
        assert pending[0].scheduled_at > datetime.now()

    async def test_a_one_shot_task_leaves_nothing_behind(self, store):
        task = await _running_task(store)
        runner = SchedulerRunner(store=store, execute_fn=_ok, task_timeout=5)

        await runner._execute_task(task.id, task.description)

        assert [t.id for t in await store.get_all(include_done=True)] == [task.id]

    async def test_a_task_removed_while_running_is_not_rescheduled(self, store):
        task = await _running_task(store, recurrence="every 1 hours")

        async def remove_midway(_d: str) -> str:
            await store.remove(task.id)
            return "ok"

        runner = SchedulerRunner(store=store, execute_fn=remove_midway, task_timeout=5)
        await runner._execute_task(task.id, task.description)

        assert await store.get_all(include_done=True) == []

    async def test_the_started_callback_gets_the_id_and_description(self, store):
        task = await _running_task(store, description="hello")
        started = MagicMock()
        runner = SchedulerRunner(
            store=store, execute_fn=_ok, on_task_started=started, task_timeout=5
        )

        await runner._execute_task(task.id, task.description)

        started.assert_called_once_with(task.id, "hello")


class TestACallbackThatRaises:
    async def test_a_failing_started_callback_does_not_stop_the_task(self, store, caplog):
        task = await _running_task(store)
        runner = SchedulerRunner(
            store=store,
            execute_fn=_ok,
            on_task_started=MagicMock(side_effect=RuntimeError("listener broke")),
            task_timeout=5,
        )

        with caplog.at_level(logging.ERROR):
            await runner._execute_task(task.id, task.description)

        assert (await store.get(task.id)).status == TaskStatus.completed
        assert "listener broke" in caplog.text

    async def test_a_failing_completed_callback_does_not_stop_the_recurrence(self, store):
        task = await _running_task(store, recurrence="every 1 hours")
        runner = SchedulerRunner(
            store=store,
            execute_fn=_ok,
            on_task_completed=MagicMock(side_effect=RuntimeError("listener broke")),
            task_timeout=5,
        )

        await runner._execute_task(task.id, task.description)

        assert (await store.get(task.id)).status == TaskStatus.completed
        assert len(await store.get_all()) == 1  # the next occurrence is pending


class TestConcurrency:
    @staticmethod
    async def _peak(store, max_concurrent: int, count: int = 4) -> int:
        for i in range(count):
            await store.add(ScheduledTask.new(f"t{i}", datetime.now() - timedelta(minutes=1)))
        live = peak = 0

        async def work(_d: str) -> str:
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.05)
            live -= 1
            return "ok"

        runner = SchedulerRunner(
            store=store, execute_fn=work, task_timeout=5, max_concurrent=max_concurrent
        )
        await runner._check_and_spawn()
        await asyncio.wait_for(asyncio.gather(*runner._active_tasks), 10)
        assert len(await store.get_all(include_done=True)) == count
        assert all(t.status == TaskStatus.completed for t in await store.get_all(include_done=True))
        return peak

    async def test_one_at_a_time_by_default(self, store):
        assert await self._peak(store, 1) == 1

    async def test_the_limit_is_the_most_that_run_together(self, store):
        assert await self._peak(store, 2) == 2

    @pytest.mark.parametrize("limit", [0, -3])
    async def test_a_limit_below_one_still_runs_tasks_one_at_a_time(self, store, limit):
        assert await self._peak(store, limit) == 1

    async def test_due_tasks_are_marked_running_before_they_start(self, store):
        task = ScheduledTask.new("t", datetime.now() - timedelta(minutes=1))
        await store.add(task)
        gate = asyncio.Event()

        async def work(_d: str) -> str:
            await gate.wait()
            return "ok"

        runner = SchedulerRunner(store=store, execute_fn=work, task_timeout=5)
        await runner._check_and_spawn()

        assert (await store.get(task.id)).status == TaskStatus.running
        assert await store.get_due_tasks() == []
        gate.set()
        await asyncio.gather(*runner._active_tasks)


class TestTheDelayBetweenChecks:
    def _runner(self, **kw):
        return SchedulerRunner(
            store=MagicMock(), execute_fn=_ok, poll_interval=kw.pop("poll_interval", 30), **kw
        )

    def test_with_nothing_pending_it_idles_for_five_minutes(self):
        assert self._runner()._next_delay() == 300.0

    def test_a_long_poll_interval_beats_the_idle_default(self):
        assert self._runner(poll_interval=900)._next_delay() == 900

    def test_a_task_due_soon_is_waited_for_exactly(self):
        runner = self._runner()
        runner._next_due = datetime.now(UTC) + timedelta(seconds=5)
        assert 3 < runner._next_delay() <= 5

    def test_a_task_due_far_away_is_checked_at_the_poll_interval(self):
        runner = self._runner()
        runner._next_due = datetime.now(UTC) + timedelta(hours=3)
        assert runner._next_delay() == 30

    def test_a_task_already_overdue_is_checked_at_once(self):
        runner = self._runner()
        runner._next_due = datetime.now(UTC) - timedelta(hours=3)
        assert runner._next_delay() == 0.0


class TestTheWake:
    async def test_the_next_due_time_is_published_in_utc(self, store):
        due = datetime.now() + timedelta(hours=2)
        await store.add(ScheduledTask.new("later", due))
        runner = SchedulerRunner(store=store, execute_fn=_ok)

        await runner._check_and_spawn()

        wakes = ActivityRegistry.shared().wakes()
        assert len(wakes) == 1
        assert wakes[0].at == due.astimezone(UTC)
        assert wakes[0].reason == "a scheduled task"
        assert runner._next_due == due.astimezone(UTC)

    async def test_nothing_pending_withdraws_the_wake(self, store):
        runner = SchedulerRunner(store=store, execute_fn=_ok)
        ActivityRegistry.shared().wake(runner._wake_holder, datetime.now(UTC), "stale")

        await runner._check_and_spawn()

        assert ActivityRegistry.shared().wakes() == []

    async def test_a_failure_reading_the_next_due_time_is_swallowed(self, store):
        task = ScheduledTask.new("due", datetime.now() - timedelta(minutes=1))
        await store.add(task)
        store.next_pending_at = AsyncMock(side_effect=RuntimeError("db hiccup"))
        runner = SchedulerRunner(store=store, execute_fn=_ok, task_timeout=5)

        await runner._check_and_spawn()  # must not raise
        await asyncio.gather(*runner._active_tasks)

        assert (await store.get(task.id)).status == TaskStatus.completed

    async def test_stopping_withdraws_the_wake(self, store):
        await store.add(ScheduledTask.new("later", datetime.now() + timedelta(hours=2)))
        runner = SchedulerRunner(store=store, execute_fn=_ok)
        await runner._check_and_spawn()

        runner.stop()

        assert ActivityRegistry.shared().wakes() == []


class TestThePollLoop:
    async def test_a_check_that_raises_is_logged_and_the_loop_goes_on(self, caplog):
        calls = 0
        second = asyncio.Event()

        async def due_tasks():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("db went away")
            second.set()
            return []

        store = MagicMock()
        store.get_due_tasks = due_tasks
        store.next_pending_at = AsyncMock(return_value=None)
        runner = SchedulerRunner(store=store, execute_fn=_ok, poll_interval=0.05)
        # A short idle sleep so the second check comes without a poke.
        runner.IDLE_POLL_SECONDS = 0.05

        with caplog.at_level(logging.ERROR):
            runner.start()
            await asyncio.wait_for(second.wait(), 5)
            await runner.aclose()

        assert "Scheduler poll error" in caplog.text
        assert calls >= 2

    async def test_a_rearm_wakes_a_sleeping_loop_early(self):
        checks: list[int] = []
        store = MagicMock()

        async def due_tasks():
            checks.append(1)
            return []

        store.get_due_tasks = due_tasks
        store.next_pending_at = AsyncMock(return_value=None)
        runner = SchedulerRunner(store=store, execute_fn=_ok, poll_interval=30)
        runner.start()
        await asyncio.sleep(0.1)
        assert len(checks) == 1  # asleep for the idle five minutes

        ActivityRegistry.shared().rearm()
        await asyncio.sleep(0.2)

        assert len(checks) == 2
        await runner.aclose()

    async def test_a_stopped_loop_no_longer_hears_a_rearm(self):
        store = MagicMock()
        store.get_due_tasks = AsyncMock(return_value=[])
        store.next_pending_at = AsyncMock(return_value=None)
        runner = SchedulerRunner(store=store, execute_fn=_ok)
        runner.start()
        await asyncio.sleep(0.05)
        await runner.aclose()

        assert ActivityRegistry.shared()._timers == []


class TestShuttingDown:
    async def test_stop_cancels_the_tasks_in_flight(self, store):
        task = ScheduledTask.new("t", datetime.now() - timedelta(minutes=1))
        await store.add(task)
        started = asyncio.Event()
        cancelled: list[bool] = []

        async def work(_d: str) -> str:
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            return "never"

        runner = SchedulerRunner(store=store, execute_fn=work, task_timeout=60)
        await runner._check_and_spawn()
        await asyncio.wait_for(started.wait(), 5)

        runner.stop()
        await asyncio.sleep(0.05)

        assert cancelled == [True]
        assert runner._active_tasks == set()

    async def test_aclose_gives_up_on_a_check_that_never_finishes(self, caplog):
        started = asyncio.Event()
        cancelled: list[bool] = []

        async def hang():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        store = MagicMock()
        store.get_due_tasks = hang
        runner = SchedulerRunner(store=store, execute_fn=_ok)
        runner.CLOSE_TIMEOUT_SECONDS = 0.1
        runner.start()
        await asyncio.wait_for(started.wait(), 5)

        with caplog.at_level(logging.WARNING):
            await asyncio.wait_for(runner.aclose(), 5)
        await asyncio.sleep(0.05)

        assert "did not finish in time" in caplog.text
        assert cancelled == [True]

    async def test_aclose_with_nothing_started_returns_at_once(self):
        runner = SchedulerRunner(store=MagicMock(), execute_fn=_ok)
        await asyncio.wait_for(runner.aclose(), 1)
        assert not runner.is_running
