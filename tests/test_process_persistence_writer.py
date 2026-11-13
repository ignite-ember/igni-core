"""What the registry's persistence writer sends to the store, and when.

Writes are scheduled, never awaited by the caller, and a store that
answers ``ok=False`` or raises must not surface in a caller that cannot
act on it. A recording fake stands in for the database; the writer is
real.
"""

from __future__ import annotations

import asyncio
import logging
import os

import pytest

from igni_core.core.tools.process_persistence import ProcessPersistenceWriter
from igni_core.core.tools.process_store_schemas import (
    BackgroundProcessRow,
    RemoveResult,
    UpsertResult,
)
from tests.process_fakes import FakeTrackedProcess


class _Store:
    def __init__(self, *, ok: bool = True, reason: str = "") -> None:
        self.ok = ok
        self.reason = reason
        self.upserts: list[BackgroundProcessRow] = []
        self.removed: list[int] = []
        self.finished: list[tuple[int, int | None, BackgroundProcessRow | None]] = []

    async def upsert(self, row: BackgroundProcessRow) -> UpsertResult:
        self.upserts.append(row)
        return UpsertResult(ok=self.ok, reason=self.reason)

    async def remove(self, pid: int) -> RemoveResult:
        self.removed.append(pid)
        return RemoveResult(ok=self.ok, reason=self.reason)

    async def finish(
        self, pid: int, exit_code: int | None, *, row: BackgroundProcessRow | None = None
    ) -> UpsertResult:
        self.finished.append((pid, exit_code, row))
        return UpsertResult(ok=self.ok, reason=self.reason)


async def _drain() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_record_start_writes_the_row_with_pid_cmd_and_group() -> None:
    store = _Store()
    writer = ProcessPersistenceWriter(store)
    pid = os.getpid()
    writer.record_start(pid, "make test")
    await _drain()
    (row,) = store.upserts
    assert (row.pid, row.cmd) == (pid, "make test")
    assert row.pgid == os.getpgid(pid)


async def test_record_start_has_no_group_where_the_platform_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(os, "getpgid", raising=False)
    store = _Store()
    ProcessPersistenceWriter(store).record_start(5, "x")
    await _drain()
    assert store.upserts[0].pgid is None


class _StartedAt(FakeTrackedProcess):
    @property
    def started_epoch(self) -> int:
        return 1_700_000_000


async def test_record_end_writes_the_whole_row_so_a_one_shot_survives() -> None:
    store = _Store()
    mp = _StartedAt(pid=11, cmd="ls -la", returncode=3, running=False)
    ProcessPersistenceWriter(store).record_end(mp)
    await _drain()
    ((pid, code, row),) = store.finished
    assert (pid, code) == (11, 3)
    assert row is not None
    assert (row.pid, row.cmd, row.pgid, row.started_at) == (11, "ls -la", None, mp.started_epoch)


async def test_record_end_passes_an_unknown_exit_code_through() -> None:
    store = _Store()
    ProcessPersistenceWriter(store).record_end(FakeTrackedProcess(pid=1, returncode=None))
    await _drain()
    assert store.finished[0][1] is None


async def test_forget_removes_the_pid() -> None:
    store = _Store()
    ProcessPersistenceWriter(store).forget(12)
    await _drain()
    assert store.removed == [12]


async def test_nothing_is_written_in_the_caller_before_it_yields() -> None:
    store = _Store()
    writer = ProcessPersistenceWriter(store)
    writer.forget(1)
    assert store.removed == []  # scheduled, not awaited
    await _drain()
    assert store.removed == [1]


async def test_no_store_means_all_three_writes_do_nothing() -> None:
    writer = ProcessPersistenceWriter()
    assert writer.store is None
    writer.record_start(1, "x")
    writer.record_end(FakeTrackedProcess())
    writer.forget(1)
    await _drain()
    assert asyncio.all_tasks() == {asyncio.current_task()}  # nothing was scheduled


async def test_attach_and_detach_rewire_the_store() -> None:
    first, second = _Store(), _Store()
    writer = ProcessPersistenceWriter(first)
    assert writer.store is first
    writer.attach(second)
    assert writer.store is second
    writer.forget(1)
    await _drain()
    assert (first.removed, second.removed) == ([], [1])
    writer.detach()
    assert writer.store is None
    writer.forget(2)
    await _drain()
    assert second.removed == [1]
    writer.attach(first)
    writer.attach(None)
    assert writer.store is None


def test_with_no_running_loop_a_write_is_dropped_without_raising() -> None:
    store = _Store()
    writer = ProcessPersistenceWriter(store)
    writer.record_start(1, "x")
    writer.record_end(FakeTrackedProcess())
    writer.forget(1)
    assert (store.upserts, store.finished, store.removed) == ([], [], [])


@pytest.mark.parametrize(
    ("act", "needle"),
    [
        (lambda w: w.record_start(os.getpid(), "x"), "persist_add failed: disk full"),
        (
            lambda w: w.record_end(FakeTrackedProcess(pid=8)),
            "persist_finish pid=8 failed: disk full",
        ),
        (lambda w: w.forget(9), "persist_remove pid=9 failed: disk full"),
    ],
)
async def test_a_failed_result_is_logged_at_debug_and_not_raised(
    act, needle: str, caplog: pytest.LogCaptureFixture
) -> None:
    writer = ProcessPersistenceWriter(_Store(ok=False, reason="disk full"))
    with caplog.at_level(logging.DEBUG, logger="igni_core.core.tools.process_persistence"):
        act(writer)
        await _drain()
    records = [r for r in caplog.records if needle in r.getMessage()]
    assert records and records[0].levelno == logging.DEBUG
