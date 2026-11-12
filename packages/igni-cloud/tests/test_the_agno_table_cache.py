"""Agno's tables, once found, are remembered per cache and never confused.

``igni_cloud.storage.agno_table_cache``: the cache one storage owns, so
Agno's existence, column and reflection round trips are paid once.
"""

from __future__ import annotations

import threading

from igni_cloud.storage.agno_table_cache import AgnoTableCache, AgnoTableKey
from sqlalchemy import Column, Integer, MetaData, Table


def _table(name: str) -> Table:
    return Table(name, MetaData(), Column("id", Integer, primary_key=True))


def _key(
    database: str = "postgresql+asyncpg://u@h/db",
    schema: str = "ai",
    table_type: str = "sessions",
    name: str = "igni_sessions",
) -> AgnoTableKey:
    return AgnoTableKey(database=database, schema=schema, table_type=table_type, name=name)


def test_a_table_never_remembered_is_not_found():
    assert AgnoTableCache().get(_key()) is None


def test_a_remembered_table_is_found_again_and_the_latest_one_wins():
    cache = AgnoTableCache()
    first, second = _table("a"), _table("b")
    cache.remember(_key(), first)
    assert cache.get(_key()) is first
    cache.remember(_key(), second)
    assert cache.get(_key()) is second


def test_keys_that_differ_in_one_field_are_kept_apart():
    cache = AgnoTableCache()
    base = _key()
    others = [
        _key(database="postgresql+asyncpg://u@h/other"),
        _key(schema="public"),
        _key(table_type="memories"),
        _key(name="igni_memories"),
    ]
    remembered = _table("base")
    cache.remember(base, remembered)
    for other in others:
        assert cache.get(other) is None
    for index, other in enumerate(others):
        cache.remember(other, _table(f"t{index}"))
    assert cache.get(base) is remembered
    assert len({cache.get(k).name for k in others}) == len(others)  # type: ignore[union-attr]


def test_equal_keys_are_one_entry():
    cache = AgnoTableCache()
    table = _table("a")
    cache.remember(_key(), table)
    assert cache.get(_key()) is table
    assert _key() == _key() and hash(_key()) == hash(_key())


def test_two_caches_share_nothing():
    first, second = AgnoTableCache(), AgnoTableCache()
    first.remember(_key(), _table("a"))
    assert second.get(_key()) is None


def test_many_threads_remembering_and_reading_lose_nothing():
    cache = AgnoTableCache()
    tables = {n: _table(f"t{n}") for n in range(8)}
    wrong: list[int] = []

    def work(n: int) -> None:
        for _ in range(500):
            cache.remember(_key(name=f"n{n}"), tables[n])
            if cache.get(_key(name=f"n{n}")) is not tables[n]:
                wrong.append(n)

    threads = [threading.Thread(target=work, args=(n,)) for n in tables]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert wrong == []
    assert all(cache.get(_key(name=f"n{n}")) is tables[n] for n in tables)
