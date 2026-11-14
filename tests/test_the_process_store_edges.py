"""The background-process store at its edges, on both dialects.

``test_process_history_survives_restart`` covers the round trip. These
pin what a caller relies on when it is not the round trip: a reused pid
is a new process, an ending can be written for a process that was never
announced, history is forgotten only past the cutoff and never while it
runs, producers sharing a database never see each other's rows, and a
database that fails is reported rather than raised.
"""

from __future__ import annotations

import uuid

import pytest

from igni_core.core.db.database import Database
from igni_core.core.db.scope import StorageScope, Tenant
from igni_core.core.tools.process_store import BackgroundProcessStore
from igni_core.core.tools.process_store_schemas import BackgroundProcessRow


def _running(pid: int, cmd: str = "sleep 9", started_at: int = 100) -> BackgroundProcessRow:
    return BackgroundProcessRow(pid=pid, cmd=cmd, pgid=pid, started_at=started_at)


@pytest.fixture
def db(database_url) -> Database:
    return Database(database_url)


def _scoped(db: Database, producer: str) -> BackgroundProcessStore:
    """A store on its own producer: the PostgreSQL database lasts the whole run."""
    scope = StorageScope.for_tenant(Tenant(org="acme", producer=producer), "proj")
    return BackgroundProcessStore(db=db.in_scope(scope))


@pytest.fixture
def store(db) -> BackgroundProcessStore:
    return _scoped(db, f"person:{uuid.uuid4()}")


async def test_a_reused_pid_is_a_new_process_with_no_ending(store):
    await store.upsert(_running(7, "make build", 100))
    await store.finish(7, 1, now=150)

    result = await store.upsert(_running(7, "npm start", 200))

    assert result.ok
    (row,) = await store.list_all()
    assert (row.cmd, row.started_at) == ("npm start", 200)
    assert row.exit_code is None
    assert row.finished_at is None
    assert not row.is_finished


async def test_an_ending_for_a_pid_never_announced_inserts_the_row_whole(store):
    """A quick ``ls`` is never announced; its ending must still persist."""
    result = await store.finish(9, 0, now=130, row=_running(9, "ls", 120))

    assert result.ok
    (row,) = await store.list_all()
    assert (row.pid, row.cmd, row.started_at) == (9, "ls", 120)
    assert (row.exit_code, row.finished_at) == (0, 130)


async def test_an_ending_for_an_unknown_pid_without_a_row_writes_nothing(store):
    result = await store.finish(9, 0, now=130)

    assert result.ok
    assert await store.list_all() == []


async def test_an_ending_overwrites_an_earlier_ending_and_keeps_the_row(store):
    await store.upsert(_running(4, "sleep 1", 100))
    await store.finish(4, 1, now=110)
    await store.finish(4, None, now=120, row=_running(4, "ignored", 1))

    (row,) = await store.list_all()
    assert row.cmd == "sleep 1", "a matching row must be updated, not replaced by the fallback"
    assert row.exit_code is None
    assert row.finished_at == 120
    assert row.is_finished


async def test_an_ending_without_a_clock_is_stamped_now(store):
    import time

    await store.upsert(_running(4))
    before = int(time.time())
    await store.finish(4, 0)

    (row,) = await store.list_all()
    assert row.finished_at is not None
    assert before <= row.finished_at <= int(time.time())


async def test_pruning_forgets_only_finished_rows_strictly_older_than_the_cutoff(store):
    await store.upsert(_running(1))
    await store.finish(1, 0, now=900)  # 100 s old: exactly the cutoff, kept
    await store.upsert(_running(2))
    await store.finish(2, 0, now=899)  # 101 s old: goes
    await store.upsert(_running(3, started_at=1))  # running, however old: kept

    removed = await store.prune_history(older_than_seconds=100, now=1000)

    assert removed == 1
    assert sorted(r.pid for r in await store.list_all()) == [1, 3]


async def test_pruning_truncates_a_fractional_window_and_zero_forgets_all_history(store):
    await store.upsert(_running(1))
    await store.finish(1, 0, now=1000)
    await store.upsert(_running(2))
    await store.upsert(_running(3))
    await store.finish(3, 0, now=998)

    # int(1.9) is 1: the cutoff is 999, so only the row that ended at 998 goes.
    assert await store.prune_history(older_than_seconds=1.9, now=1000) == 1
    assert sorted(r.pid for r in await store.list_all()) == [1, 2]
    # A zero window forgets everything that has ended before now.
    assert await store.prune_history(older_than_seconds=0, now=1001) == 1
    assert [r.pid for r in await store.list_all()] == [2]


async def test_a_negative_window_forgets_history_that_has_not_happened_yet(store):
    await store.upsert(_running(1))
    await store.finish(1, 0, now=1000)

    assert await store.prune_history(older_than_seconds=-5, now=1000) == 1


async def test_removing_says_whether_a_row_was_there(store):
    await store.upsert(_running(5))

    first = await store.remove(5)
    second = await store.remove(5)

    assert (first.ok, first.removed) == (True, True)
    assert (second.ok, second.removed) == (True, False)


async def test_rows_come_back_oldest_first(store):
    await store.upsert(_running(1, started_at=300))
    await store.upsert(_running(2, started_at=100))
    await store.upsert(_running(3, started_at=200))

    assert [r.pid for r in await store.list_all()] == [2, 3, 1]


async def test_unicode_and_very_long_commands_round_trip(store):
    long_cmd = "echo " + "é漢" * 5000
    await store.upsert(_running(1, long_cmd))

    (row,) = await store.list_all()
    assert row.cmd == long_cmd


async def test_a_pid_past_32_bits_is_not_a_failure_on_sqlite(db, store):
    if db.url.file is None:
        pytest.skip("PostgreSQL's Integer column is 32-bit; the pid limit there is the OS's")
    assert (await store.upsert(_running(2**31 + 5))).ok


class TestTwoProducersOnOneDatabase:
    @pytest.fixture
    def stores(self, db):
        run = uuid.uuid4()
        return _scoped(db, f"person:a-{run}"), _scoped(db, f"person:b-{run}")

    async def test_the_same_pid_in_two_scopes_is_two_rows(self, stores):
        a, b = stores
        await a.upsert(_running(7, "a-job"))
        await b.upsert(_running(7, "b-job"))

        assert [r.cmd for r in await a.list_all()] == ["a-job"]
        assert [r.cmd for r in await b.list_all()] == ["b-job"]

    async def test_neither_can_end_prune_or_remove_the_others_rows(self, stores):
        a, b = stores
        await a.upsert(_running(7, "a-job"))
        await b.upsert(_running(7, "b-job"))
        await b.finish(7, 0, now=10)

        assert [r.exit_code for r in await a.list_all()] == [None]
        assert await a.prune_history(older_than_seconds=0, now=10_000) == 0
        assert [r.cmd for r in await b.list_all()] == ["b-job"]
        assert (await b.remove(7)).removed
        assert [r.cmd for r in await a.list_all()] == ["a-job"]
        assert not (await b.remove(7)).removed


async def test_a_driver_that_cannot_count_rows_is_taken_to_have_removed_one():
    class Result:
        rowcount = -1

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, _stmt):
            return Result()

        async def commit(self):
            pass

    class Counting:
        scope = StorageScope.local()

        def session(self):
            return Session()

    result = await BackgroundProcessStore(db=Counting()).remove(1)  # type: ignore[arg-type]

    assert result.ok
    assert result.removed


class TestADatabaseThatFails:
    @pytest.fixture
    def broken(self, tmp_path):
        class Broken:
            scope = StorageScope.local()

            def session(self):
                raise RuntimeError("disk gone")

        return BackgroundProcessStore(db=Broken())  # type: ignore[arg-type]

    async def test_writes_report_instead_of_raising(self, broken):
        up = await broken.upsert(_running(3))
        fin = await broken.finish(3, 0, now=1)
        rem = await broken.remove(3)

        assert not up.ok and up.reason == "upsert(pid=3): disk gone"
        assert not fin.ok and fin.reason == "finish(pid=3): disk gone"
        assert not rem.ok and not rem.removed and rem.reason == "remove(pid=3): disk gone"

    async def test_pruning_reports_nothing_removed(self, broken):
        assert await broken.prune_history(older_than_seconds=0, now=5) == 0

    async def test_listing_raises_to_its_caller(self, broken):
        with pytest.raises(RuntimeError, match="disk gone"):
            await broken.list_all()
