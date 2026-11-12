"""What ``igni.storage``'s ``postgresql`` provider hands out and prepares.

``igni_cloud.storage.postgres``: a producer's Agno storage (scoped, one
table cache for the process, over the URL's engine, never making a
table), its uploads, and what the admin's ``igni migrate`` prepares.
Each test needs ``IGNI_TEST_DATABASE_URL``.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from agno.db.base import SessionType
from igni_cloud.storage.attachments import LargeObjectAttachments
from igni_cloud.storage.postgres import PostgresStorage
from igni_cloud.storage.scoped_agno import ScopedAsyncPostgresDb
from sqlalchemy import event, text

from igni_core.core.db.database import Database
from igni_core.core.db.engine import engines
from igni_core.core.db.urls import DatabaseUrl
from igni_core.core.memory.manager import StorageManager
from tests.storage_backends import Scopes, ThrowawayPostgres

SESSIONS = StorageManager.SESSION_TABLE
MEMORIES = StorageManager.MEMORY_TABLE


def _agno_db(storage: PostgresStorage, url: DatabaseUrl, scope=None) -> ScopedAsyncPostgresDb:
    db = storage.agno_db(
        url=url,
        scope=scope or Scopes.fresh(),
        session_table=SESSIONS,
        memory_table=MEMORIES,
    )
    assert isinstance(db, ScopedAsyncPostgresDb)
    return db


def _flags(url: DatabaseUrl) -> dict[str, bool]:
    with engines.get_engine(url).connect() as connection:
        rows = connection.execute(
            text(
                "SELECT n.nspname || '.' || c.relname, c.relrowsecurity AND c.relforcerowsecurity "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind = 'r' AND n.nspname IN ('public', 'ai')"
            )
        )
        return {name: bool(flag) for name, flag in rows}


def test_the_agno_storage_is_scoped_over_the_urls_engine_and_makes_no_table(
    postgres_url: str,
):
    url = DatabaseUrl.parse(postgres_url)
    scope = Scopes.fresh()
    db = _agno_db(PostgresStorage(), url, scope)
    assert db.scoped_user_id == scope.agno_user_id
    assert db.db_engine is engines.get_async_engine(url)
    assert db.create_schema is False
    assert db.session_table_name == SESSIONS and db.memory_table_name == MEMORIES


def test_a_table_that_was_never_prepared_is_not_made_by_a_producer():
    server = os.environ.get("IGNI_TEST_DATABASE_URL", "").strip()
    if not server:
        pytest.skip("IGNI_TEST_DATABASE_URL is unset")
    database = ThrowawayPostgres(server)
    url = DatabaseUrl.parse(database.create())
    try:

        async def read() -> object:
            try:
                return await _agno_db(PostgresStorage(), url).get_session(
                    "s1", SessionType.AGENT
                )
            finally:
                await engines.reset_pools()

        try:
            asyncio.run(read())
        except Exception:  # noqa: BLE001 - whatever Agno says of a missing table
            pass
        with engines.get_engine(url).connect() as connection:
            made = connection.scalar(
                text("SELECT count(*) FROM pg_tables WHERE tablename = :name"),
                {"name": SESSIONS},
            )
        assert made == 0
    finally:
        database.drop()


def test_every_agno_db_of_one_storage_shares_its_table_lookups(postgres_url: str):
    url = DatabaseUrl.parse(postgres_url)
    Database(url)
    PostgresStorage().prepare(url)
    statements: list[str] = []

    def heard(_conn, _cursor, statement, *_args) -> None:
        statements.append(statement)

    async def reads(storage: PostgresStorage) -> tuple[int, int, int]:
        engine = engines.get_async_engine(url)
        event.listen(engine.sync_engine, "before_cursor_execute", heard)
        try:
            await _agno_db(storage, url).get_session("a", SessionType.AGENT)
            first = len(statements)
            statements.clear()
            await _agno_db(storage, url).get_session("b", SessionType.AGENT)
            second = len(statements)
            statements.clear()
            await _agno_db(PostgresStorage(), url).get_session("c", SessionType.AGENT)
            other = len(statements)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", heard)
            await engines.reset_pools()
        return first, second, other

    first, second, other = asyncio.run(reads(PostgresStorage()))
    assert first > 2
    assert second == 1, "a second scope on the same storage reuses what was found"
    assert other > 2, "another storage owns another cache and looks again"


def test_prepare_makes_agno_tables_and_row_level_security_and_can_run_twice(
    postgres_url: str,
):
    url = DatabaseUrl.parse(postgres_url)
    Database(url)
    storage = PostgresStorage()
    storage.prepare(url)
    storage.prepare(url)
    flags = _flags(url)
    for table in (f"ai.{SESSIONS}", f"ai.{MEMORIES}", f"ai.{SESSIONS}_runs", "public.attachments"):
        assert flags.get(table) is True, f"{table} must have forced row-level security"


def test_it_hands_out_large_object_uploads_for_the_project(postgres_url: str, tmp_path: Path):
    db = Database(DatabaseUrl.parse(postgres_url), scope=Scopes.fresh())
    store = PostgresStorage().attachments(db, tmp_path)
    assert isinstance(store, LargeObjectAttachments)
    stored = store.save("s1", "a.txt", [b"hi"])
    path = store.local_path(stored.ref)
    assert path is not None and path.is_relative_to(tmp_path)
