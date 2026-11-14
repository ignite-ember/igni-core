"""What :class:`EngineRegistry` does to every connection, and how it lets them go.

A SQLite connection waits five seconds on a lock and is in WAL mode, on the
sync and the async engine alike; a PostgreSQL connection carries the connect
timeout and keepalives; the registry hands one engine to every thread that
asks first; ``dispose`` awaits the async engines before it clears the
caches; and ``close_connections`` and ``reset_pools`` work on any registry.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from igni_core.core.db.engine import engines
from igni_core.core.db.engine_registry import (
    EngineRegistry,
    PostgresConnectionSetup,
    SqliteConnectionSetup,
)
from igni_core.core.db.urls import DatabaseUrl


@pytest.fixture
def registry():
    registry = EngineRegistry()
    yield registry
    registry.dispose_sync()


def test_a_sync_sqlite_connection_is_in_wal_mode_with_a_five_second_wait(
    registry: EngineRegistry, tmp_path: Path
):
    with registry.get_engine(tmp_path / "state.db").connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert connection.execute(text("PRAGMA busy_timeout")).scalar() == 5000


async def test_an_async_sqlite_connection_is_in_wal_mode_with_a_five_second_wait(
    registry: EngineRegistry, tmp_path: Path
):
    async with registry.get_async_engine(tmp_path / "state.db").connect() as connection:
        assert (await connection.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
        assert (await connection.execute(text("PRAGMA busy_timeout"))).scalar() == 5000


def test_a_waiting_writer_gets_the_lock_when_the_holder_lets_go(
    registry: EngineRegistry, tmp_path: Path
):
    """The busy timeout is what makes a contended write wait instead of failing."""
    path = tmp_path / "state.db"
    engine = registry.get_engine(path)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE t (n INTEGER)"))
    holder = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    releaser = threading.Timer(0.5, lambda: holder.execute("COMMIT"))
    releaser.start()
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO t VALUES (1)"))
    finally:
        releaser.join()
        holder.close()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT n FROM t")).scalars().all() == [1]


def test_the_sqlite_setup_hands_the_driver_its_timeout():
    assert SqliteConnectionSetup.connect_args() == {
        "timeout": SqliteConnectionSetup.BUSY_TIMEOUT_SECONDS
    }
    assert SqliteConnectionSetup.BUSY_TIMEOUT_SECONDS == 5.0


class _Cursor:
    def __init__(self, fails: bool) -> None:
        self.fails = fails
        self.closed = False
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(statement)
        if self.fails:
            raise sqlite3.OperationalError("disk I/O error")

    def close(self) -> None:
        self.closed = True


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _Cursor:
        return self._cursor


def test_the_wal_pragma_cursor_is_closed_when_it_works():
    cursor = _Cursor(fails=False)
    SqliteConnectionSetup.on_connect(_Connection(cursor), None)
    assert cursor.statements == ["PRAGMA journal_mode=WAL"]
    assert cursor.closed


def test_the_wal_pragma_cursor_is_closed_when_it_fails_and_the_failure_surfaces():
    cursor = _Cursor(fails=True)
    with pytest.raises(sqlite3.OperationalError):
        SqliteConnectionSetup.on_connect(_Connection(cursor), None)
    assert cursor.closed


def test_a_postgresql_connection_fails_fast_and_keeps_alive():
    args: dict[str, Any] = PostgresConnectionSetup.connect_args()
    assert args == {
        "connect_timeout": 10,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
        "tcp_user_timeout": 30_000,
        "options": (
            "-c tcp_keepalives_idle=30 -c tcp_keepalives_interval=10 -c tcp_keepalives_count=3"
        ),
    }


def test_threads_that_ask_first_at_once_all_get_one_engine(
    registry: EngineRegistry, tmp_path: Path
):
    path = tmp_path / "state.db"
    start = threading.Barrier(8)
    got: list[object] = []

    def ask() -> None:
        start.wait()
        got.append(registry.get_engine(path))

    threads = [threading.Thread(target=ask) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(got) == 8
    assert len({id(engine) for engine in got}) == 1


def test_sessions_do_not_expire_what_they_committed(registry: EngineRegistry, tmp_path: Path):
    path = tmp_path / "state.db"
    with registry.get_engine(path).begin() as connection:
        connection.execute(text("CREATE TABLE t (n INTEGER)"))
    session = registry.get_sessionmaker(path)()
    assert session.expire_on_commit is False
    session.close()


async def test_async_sessions_do_not_expire_what_they_committed(
    registry: EngineRegistry, tmp_path: Path
):
    session = registry.get_async_sessionmaker(tmp_path / "state.db")()
    assert session.sync_session.expire_on_commit is False
    await session.close()


async def test_dispose_closes_the_async_engines_connections_and_clears_all_four_caches(
    registry: EngineRegistry, tmp_path: Path
):
    path = tmp_path / "state.db"
    async_engine = registry.get_async_engine(path)
    sync_engine = registry.get_engine(path)
    registry.get_sessionmaker(path)
    registry.get_async_sessionmaker(path)
    async with async_engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    with sync_engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    assert async_engine.pool.checkedin() == 1
    assert sync_engine.pool.checkedin() == 1
    await registry.dispose()
    assert async_engine.pool.checkedin() == 0
    assert sync_engine.pool.checkedin() == 0
    assert registry.get_engine(path) is not sync_engine
    assert registry.get_async_engine(path) is not async_engine
    assert registry.get_sessionmaker(path).kw["bind"] is registry.get_engine(path)
    assert registry.get_async_sessionmaker(path).kw["bind"] is registry.get_async_engine(path)


async def test_an_empty_registry_can_be_paused_restored_and_disposed():
    registry = EngineRegistry()
    await registry.close_connections()
    await registry.reset_pools()
    await registry.dispose()


async def test_pausing_a_sqlite_registry_keeps_its_engines_and_the_next_query_works(
    registry: EngineRegistry, tmp_path: Path
):
    path = tmp_path / "state.db"
    sync_engine = registry.get_engine(path)
    async_engine = registry.get_async_engine(path)
    with sync_engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    async with async_engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    assert sync_engine.pool.checkedin() == 1
    assert async_engine.pool.checkedin() == 1
    await registry.close_connections()
    assert sync_engine.pool.checkedin() == 0
    assert async_engine.pool.checkedin() == 0
    assert registry.get_engine(path) is sync_engine
    assert registry.get_async_engine(path) is async_engine
    with sync_engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar() == 1
    async with async_engine.connect() as connection:
        assert (await connection.execute(text("SELECT 1"))).scalar() == 1


async def test_a_restored_sqlite_registry_recycles_its_connections_at_the_next_use(
    registry: EngineRegistry, tmp_path: Path
):
    engine = registry.get_engine(tmp_path / "state.db")
    with engine.connect() as connection:
        before = connection.connection.dbapi_connection
    await registry.reset_pools()
    with engine.connect() as connection:
        after = connection.connection.dbapi_connection
        assert connection.execute(text("SELECT 1")).scalar() == 1
    assert after is not before


def test_the_process_registry_is_the_one_every_database_handle_draws_from(tmp_path: Path):
    from igni_core.core.db.database import Database

    path = tmp_path / "state.db"
    database = Database(path)
    url = DatabaseUrl.for_file(path)
    assert database._sessionmaker is engines.get_async_sessionmaker(url)
    assert database._sync_sessionmaker is engines.get_sessionmaker(url)
    assert database._sessionmaker.kw["bind"] is engines.get_async_engine(url)
    assert isinstance(engines, EngineRegistry)
