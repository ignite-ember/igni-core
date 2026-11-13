"""The background-process registry's coordination, through its public surface.

The registry holds the pid map and coordinates three collaborators: the
bus it announces on, the writer that persists rows and the eviction
timers. These tests use the real registry, bus, log store, eviction and
persistence writer; only the database (a recording store) and the
operating-system sleep inhibitor (a counting subclass) are stood in for.
"""

from __future__ import annotations

import asyncio

import pytest

from igni_core.core.tools.process_events import (
    ProcessExitEvent,
    ProcessLineEvent,
    ProcessStartEvent,
)
from igni_core.core.tools.process_log import ProcessLogStore
from igni_core.core.tools.process_registry import (
    DEFAULT_FINISHED_PROCESS_TTL_SECONDS,
    ProcessRegistry,
)
from igni_core.core.tools.process_registry_schemas import KnownProcess, RunningProcess
from igni_core.core.tools.process_store_schemas import (
    BackgroundProcessRow,
    RemoveResult,
    UpsertResult,
)
from igni_core.core.tools.sleep_blocker import SleepBlocker
from tests.process_fakes import FakeTrackedProcess


class _CountingBlocker(SleepBlocker):
    def __init__(self) -> None:
        super().__init__()
        self.acquired = 0
        self.released = 0

    def acquire(self) -> None:
        self.acquired += 1

    def release(self) -> None:
        self.released += 1


class _Store:
    def __init__(self) -> None:
        self.upserts: list[BackgroundProcessRow] = []
        self.removed: list[int] = []
        self.finished: list[tuple[int, int | None]] = []

    async def upsert(self, row: BackgroundProcessRow) -> UpsertResult:
        self.upserts.append(row)
        return UpsertResult(ok=True)

    async def remove(self, pid: int) -> RemoveResult:
        self.removed.append(pid)
        return RemoveResult(ok=True)

    async def finish(
        self, pid: int, exit_code: int | None, *, row: BackgroundProcessRow | None = None
    ) -> UpsertResult:
        self.finished.append((pid, exit_code))
        return UpsertResult(ok=True)


async def _drain() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


class TestDefaults:
    def test_a_bare_registry_has_a_fresh_bus_log_store_and_no_persistence(self) -> None:
        a, b = ProcessRegistry(), ProcessRegistry()
        assert a.bus is not b.bus
        assert a.log_store.project_dir is None
        assert a.persistence is None
        assert a.ttl_seconds == DEFAULT_FINISHED_PROCESS_TTL_SECONDS == 600.0

    def test_given_collaborators_are_the_ones_used(self, tmp_path) -> None:
        store = ProcessLogStore(tmp_path)
        registry = ProcessRegistry(log_store=store, ttl_seconds=3.0)
        assert registry.log_store is store
        assert registry.ttl_seconds == 3.0

    def test_the_ttl_and_persistence_can_be_changed_later(self) -> None:
        registry = ProcessRegistry()
        registry.set_ttl_seconds(9.0)
        store = _Store()
        registry.attach_persistence(store)
        assert (registry.ttl_seconds, registry.persistence) == (9.0, store)
        registry.attach_persistence(None)
        assert registry.persistence is None


class TestAddGetRemove:
    async def test_add_makes_it_readable_and_neither_emits_nor_persists(self) -> None:
        store = _Store()
        registry = ProcessRegistry(persistence=store)
        events: list[object] = []
        registry.bus.on("start", events.append)
        mp = FakeTrackedProcess(pid=5)
        assert registry.add(mp) == 5
        await _drain()
        assert registry.get(5) is mp
        assert events == [] and store.upserts == []

    def test_an_unknown_pid_is_none(self) -> None:
        assert ProcessRegistry().get(1) is None

    async def test_remove_drops_the_entry_deletes_the_row_and_cancels_eviction(self) -> None:
        store = _Store()
        registry = ProcessRegistry(persistence=store, ttl_seconds=0.05)
        mp = FakeTrackedProcess(pid=6, running=False, returncode=0)
        registry.add(mp)
        registry.arm_eviction(mp)
        registry.remove(6)
        await _drain()
        assert registry.get(6) is None
        assert store.removed == [6]
        assert registry._eviction._tasks == {}

    async def test_remove_of_an_unknown_pid_is_fine(self) -> None:
        registry = ProcessRegistry()
        registry.remove(404)
        registry.remove(404)


class TestAnnounceStart:
    async def test_it_persists_acquires_the_blocker_and_emits_a_wall_clock_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, blocker = _Store(), _CountingBlocker()
        registry = ProcessRegistry(persistence=store, sleep_blocker=blocker)
        got: list[ProcessStartEvent] = []
        registry.bus.on("start", got.append)
        monkeypatch.setattr("igni_core.core.tools.process_registry.time.time", lambda: 1234.5)
        registry.announce_start(FakeTrackedProcess(pid=7, cmd="npm run dev"))
        await _drain()
        assert [(r.pid, r.cmd) for r in store.upserts] == [(7, "npm run dev")]
        assert blocker.acquired == 1
        assert got == [ProcessStartEvent(pid=7, cmd="npm run dev", started_at=1234.5)]

    async def test_the_row_is_scheduled_before_a_subscriber_runs_and_a_raising_one_cannot_sink_it(
        self,
    ) -> None:
        store = _Store()
        registry = ProcessRegistry(persistence=store)
        tasks_seen: list[int] = []

        def crashes(_event: ProcessStartEvent) -> None:
            tasks_seen.append(len(asyncio.all_tasks()))
            raise RuntimeError("the front end went away")

        registry.bus.on("start", crashes)
        registry.announce_start(FakeTrackedProcess(pid=8))
        await _drain()
        assert tasks_seen == [2]  # this test's task and the write already scheduled
        assert [r.pid for r in store.upserts] == [8]

    def test_the_blocker_is_held_until_a_completion_releases_it(self) -> None:
        blocker = _CountingBlocker()
        registry = ProcessRegistry(sleep_blocker=blocker)
        registry.announce_start(FakeTrackedProcess(pid=1))
        registry.announce_start(FakeTrackedProcess(pid=2))
        assert (blocker.acquired, blocker.released) == (2, 0)


class TestCompletion:
    async def test_emit_completion_persists_releases_and_emits_the_exit(self) -> None:
        store, blocker = _Store(), _CountingBlocker()
        registry = ProcessRegistry(persistence=store, sleep_blocker=blocker)
        got: list[ProcessExitEvent] = []
        registry.bus.on("exit", got.append)
        output = [f"line {i}" for i in range(100)]
        mp = FakeTrackedProcess(
            pid=9, cmd="make", output=output, returncode=2, running=False, elapsed_seconds=4.5
        )
        registry.emit_completion(mp)
        await _drain()
        assert store.finished == [(9, 2)]
        assert blocker.released == 1
        (event,) = got
        assert (event.pid, event.cmd, event.exit_code, event.duration_seconds) == (
            9,
            "make",
            2,
            4.5,
        )
        assert event.output_tail == "\n".join(output[-40:])
        assert event.output_tail.count("\n") == 39

    async def test_an_unknown_exit_code_is_reported_as_none(self) -> None:
        registry = ProcessRegistry()
        got: list[ProcessExitEvent] = []
        registry.bus.on("exit", got.append)
        registry.emit_completion(FakeTrackedProcess(pid=1, returncode=None, running=False))
        assert got[0].exit_code is None

    async def test_the_ending_is_recorded_not_deleted_and_the_entry_stays_listed(self) -> None:
        store = _Store()
        registry = ProcessRegistry(persistence=store)
        mp = FakeTrackedProcess(pid=3, returncode=1, running=False)
        registry.add(mp)
        registry.emit_completion(mp)
        await _drain()
        assert store.removed == []
        assert registry.get(3) is mp

    async def test_record_finished_writes_the_ending_and_arms_eviction_but_emits_nothing(
        self,
    ) -> None:
        store, blocker = _Store(), _CountingBlocker()
        registry = ProcessRegistry(persistence=store, sleep_blocker=blocker)
        events: list[object] = []
        for name in ("start", "line", "exit"):
            registry.bus.on(name, events.append)  # type: ignore[arg-type]
        mp = FakeTrackedProcess(pid=4, returncode=0, running=False)
        registry.add(mp)
        registry.record_finished(mp)
        await _drain()
        assert store.finished == [(4, 0)]
        assert events == []
        assert (blocker.acquired, blocker.released) == (0, 0)
        assert set(registry._eviction._tasks) == {4}
        registry.clear()

    async def test_emit_line_emits_a_line_event(self) -> None:
        registry = ProcessRegistry()
        got: list[ProcessLineEvent] = []
        registry.bus.on("line", got.append)
        registry.emit_line(12, "compiled")
        assert got == [ProcessLineEvent(pid=12, line="compiled")]


class TestQueries:
    def test_all_running_lists_only_the_running_with_their_elapsed_time(self) -> None:
        registry = ProcessRegistry()
        registry.add(FakeTrackedProcess(pid=1, cmd="a", elapsed_seconds=2.0))
        registry.add(FakeTrackedProcess(pid=2, cmd="b", running=False, returncode=0))
        assert registry.all_running() == [RunningProcess(pid=1, cmd="a", elapsed_seconds=2.0)]

    def test_all_known_lists_finished_ones_too_with_their_exit_code(self) -> None:
        registry = ProcessRegistry()
        registry.add(FakeTrackedProcess(pid=1, cmd="a", elapsed_seconds=2.0))
        registry.add(
            FakeTrackedProcess(pid=2, cmd="b", running=False, returncode=7, elapsed_seconds=3.0)
        )
        assert registry.all_known() == [
            KnownProcess(pid=1, cmd="a", elapsed_seconds=2.0, is_running=True, exit_code=None),
            KnownProcess(pid=2, cmd="b", elapsed_seconds=3.0, is_running=False, exit_code=7),
        ]

    def test_an_empty_registry_knows_nothing(self) -> None:
        registry = ProcessRegistry()
        assert registry.all_known() == [] and registry.all_running() == []

    def test_a_running_process_reports_no_exit_code_even_if_the_handle_has_one(self) -> None:
        registry = ProcessRegistry()
        registry.add(FakeTrackedProcess(pid=1, returncode=5, running=True))
        assert registry.all_known()[0].exit_code is None


class TestKillAndClear:
    async def test_kill_all_kills_the_running_returns_the_count_and_empties_the_map(self) -> None:
        registry = ProcessRegistry(ttl_seconds=0.05)
        live = [FakeTrackedProcess(pid=1), FakeTrackedProcess(pid=2)]
        done = FakeTrackedProcess(pid=3, running=False, returncode=0)
        for mp in (*live, done):
            registry.add(mp)
        registry.arm_eviction(done)
        assert registry.kill_all() == 2
        assert [mp._proc.killed for mp in live] == [True, True]
        assert done._proc.killed is False
        assert registry.all_known() == []
        assert registry._eviction._tasks == {}

    def test_kill_all_with_nothing_running_is_zero(self) -> None:
        assert ProcessRegistry().kill_all() == 0

    async def test_clear_drops_entries_and_pending_evictions(self) -> None:
        registry = ProcessRegistry(ttl_seconds=0.05)
        mp = FakeTrackedProcess(pid=1, running=False, returncode=0)
        registry.add(mp)
        registry.arm_eviction(mp)
        registry.clear()
        await asyncio.sleep(0.15)
        assert registry.get(1) is None and registry._eviction._tasks == {}

    async def test_reset_clears_subscribers_entries_and_persistence(self) -> None:
        registry = ProcessRegistry(persistence=_Store())
        registry.bus.on("line", lambda _e: None)
        registry.add(FakeTrackedProcess(pid=1))
        registry.reset()
        assert registry.bus.subscriber_count("line") == 0
        assert registry.all_known() == []
        assert registry.persistence is None


class TestEviction:
    async def test_a_finished_entry_and_its_log_leave_when_the_ttl_runs_out(self, tmp_path) -> None:
        logs = ProcessLogStore(tmp_path)
        registry = ProcessRegistry(log_store=logs, ttl_seconds=0.02)
        mp = FakeTrackedProcess(pid=21, running=False, returncode=0)
        handle = logs.open(21)
        assert handle is not None
        handle.write("output\n")
        handle.close()
        registry.add(mp)
        registry.arm_eviction(mp)
        assert registry.get(21) is mp and logs.path(21).exists()
        await asyncio.sleep(0.15)
        assert registry.get(21) is None
        assert not logs.path(21).exists()
        assert registry._eviction._tasks == {}

    async def test_a_reused_pid_is_not_evicted_by_the_old_timer(self, tmp_path) -> None:
        logs = ProcessLogStore(tmp_path)
        registry = ProcessRegistry(log_store=logs, ttl_seconds=0.02)
        old = FakeTrackedProcess(pid=22, running=False, returncode=0)
        registry.add(old)
        registry.arm_eviction(old)
        replacement = FakeTrackedProcess(pid=22, cmd="new one")
        registry.add(replacement)
        handle = logs.open(22)
        assert handle is not None
        handle.write("the new process's output\n")
        handle.close()
        await asyncio.sleep(0.15)
        assert registry.get(22) is replacement
        assert logs.lines(22) == ["the new process's output"]

    async def test_re_arming_pushes_the_expiry_back(self) -> None:
        registry = ProcessRegistry(ttl_seconds=0.3)
        mp = FakeTrackedProcess(pid=23, running=False, returncode=0)
        registry.add(mp)
        registry.arm_eviction(mp)
        await asyncio.sleep(0.2)
        registry.arm_eviction(mp)
        await asyncio.sleep(0.2)
        assert registry.get(23) is mp
        await asyncio.sleep(0.3)
        assert registry.get(23) is None

    async def test_a_ttl_change_applies_to_the_next_arm(self) -> None:
        registry = ProcessRegistry(ttl_seconds=60.0)
        registry.set_ttl_seconds(0.02)
        mp = FakeTrackedProcess(pid=24, running=False, returncode=0)
        registry.add(mp)
        registry.arm_eviction(mp)
        await asyncio.sleep(0.15)
        assert registry.get(24) is None
