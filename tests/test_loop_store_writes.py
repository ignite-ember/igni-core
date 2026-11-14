"""What the loop stores write: one row per key, races included, and the typed accessors.

Runs on SQLite always and on PostgreSQL when the slot names a server
(``database_url`` in ``conftest.py``).
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from igni_core.core.db.database import Database
from igni_core.core.db.engine import engines
from igni_core.core.db.urls import DatabaseUrl
from igni_core.core.loop.db_models import LoopProgressModel, LoopStateModel
from igni_core.core.loop.models import LoopState
from igni_core.core.loop.store import LoopProgressStore, LoopStore
from tests.storage_backends import Scopes


def _db(url: DatabaseUrl) -> Database:
    """A handle on a scope nobody else in the run uses (a server's database is shared)."""
    return Database(url, scope=Scopes.fresh())


def _state(**over) -> LoopState:
    fields = {
        "run_id": "run-1",
        "prompt": "p",
        "iteration_index": 1,
        "iterations_remaining": 4,
    }
    return LoopState(**{**fields, **over})


async def _a_pool_of_this_loop(url: DatabaseUrl) -> Database:
    """A handle whose connection pool this test's event loop owns.

    A server's engine is cached per URL for the whole run; contended
    checkouts wait on a queue bound to the loop that first used it.
    """
    await engines.dispose()
    return _db(url)


async def _rows(db: Database, model):
    async with db.session() as session:
        return list((await session.execute(select(model).where(db.scope.where(model)))).scalars())


async def test_concurrent_progress_writes_to_a_new_key_leave_one_row(database_url: DatabaseUrl):
    db = await _a_pool_of_this_loop(database_url)
    progress = LoopProgressStore(db=db)
    await asyncio.gather(*[progress.set("run", "k", str(i)) for i in range(20)])
    rows = await progress.list("run")
    assert len(rows) == 1
    assert rows[0][0] == "k"
    assert int(rows[0][1]) in range(20)


async def test_concurrent_first_saves_leave_one_loop_row(database_url: DatabaseUrl):
    db = await _a_pool_of_this_loop(database_url)
    store = LoopStore(db=db)
    await asyncio.gather(*[store.save(_state(iteration_index=i)) for i in range(20)])
    assert len(await _rows(db, LoopStateModel)) == 1
    assert (await store.load()).iteration_index in range(20)


async def test_a_second_save_keeps_created_at_and_moves_updated_at(database_url: DatabaseUrl):
    db = _db(database_url)
    store = LoopStore(db=db)
    await store.save(_state())
    (first,) = await _rows(db, LoopStateModel)
    created, updated = first.created_at, first.updated_at
    await asyncio.sleep(0.01)
    await store.save(_state(iteration_index=2))
    (second,) = await _rows(db, LoopStateModel)
    assert second.created_at == created
    assert second.updated_at > updated


async def test_a_second_progress_write_keeps_created_at_and_moves_updated_at(
    database_url: DatabaseUrl,
):
    db = _db(database_url)
    progress = LoopProgressStore(db=db)
    await progress.set("run", "k", "a")
    (first,) = await _rows(db, LoopProgressModel)
    created, updated = first.created_at, first.updated_at
    await asyncio.sleep(0.01)
    await progress.set("run", "k", "b")
    (second,) = await _rows(db, LoopProgressModel)
    assert second.value == "b"
    assert second.created_at == created
    assert second.updated_at > updated


@pytest.mark.parametrize("explicit", [True, False])
async def test_cap_explicit_round_trips_through_the_store(
    database_url: DatabaseUrl, explicit: bool
):
    store = LoopStore(db=_db(database_url))
    await store.save(_state(cap_explicit=not explicit))
    await store.save(_state(cap_explicit=explicit))
    assert (await store.load()).cap_explicit is explicit


async def test_unicode_and_very_long_values_round_trip(database_url: DatabaseUrl):
    db = _db(database_url)
    prompt = "проверь все файлы — 日本語 🚀\n" * 5000
    await LoopStore(db=db).save(_state(prompt=prompt))
    assert (await LoopStore(db=db).load()).prompt == prompt
    progress = LoopProgressStore(db=db)
    await progress.set("run", "ключ-🔑", prompt)
    assert await progress.get("run", "ключ-🔑") == prompt


async def test_an_empty_value_reads_back_as_empty_not_missing(database_url: DatabaseUrl):
    progress = LoopProgressStore(db=_db(database_url))
    await progress.set("run", "k", "")
    assert await progress.get("run", "k") == ""
    assert await progress.get("run", "other") is None


async def test_the_same_key_in_two_runs_stays_separate_and_delete_touches_one(
    database_url: DatabaseUrl,
):
    progress = LoopProgressStore(db=_db(database_url))
    await progress.set("a", "k", "1")
    await progress.set("b", "k", "2")
    assert await progress.delete("a", "k") is True
    assert await progress.get("a", "k") is None
    assert await progress.get("b", "k") == "2"
    assert await progress.clear("a") == 0
    assert await progress.clear("b") == 1


# ── The announced total ──────────────────────────────────────────


class TestAnnouncedTotal:
    @pytest.fixture
    def progress(self, database_url: DatabaseUrl) -> LoopProgressStore:
        return LoopProgressStore(db=_db(database_url))

    def test_it_lives_under_a_reserved_key(self):
        assert LoopProgressStore.ANNOUNCED_TOTAL_KEY == "__loop_total__"

    async def test_unset_is_none(self, progress):
        assert await progress.get_announced_total("run") is None

    async def test_it_round_trips_and_is_stored_as_text(self, progress):
        await progress.set_announced_total("run", 12)
        assert await progress.get_announced_total("run") == 12
        assert await progress.get("run", "__loop_total__") == "12"
        assert await progress.list("run") == [("__loop_total__", "12")]

    async def test_it_is_per_run(self, progress):
        await progress.set_announced_total("a", 3)
        assert await progress.get_announced_total("b") is None

    @pytest.mark.parametrize("raw", ["", "abc", "3.5", " "])
    async def test_an_empty_or_non_integer_value_reads_as_none(self, progress, raw):
        await progress.set("run", "__loop_total__", raw)
        assert await progress.get_announced_total("run") is None

    @pytest.mark.parametrize("total", [0, -4])
    async def test_zero_and_negative_come_back_as_stored(self, progress, total):
        await progress.set_announced_total("run", total)
        assert await progress.get_announced_total("run") == total
