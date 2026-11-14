"""The core's storage seams, handed a PostgreSQL URL.

These build a PostgreSQL engine, so they need psycopg — the cloud layer's
driver. The core's own suite runs without it (the desktop's wheel carries no
psycopg), so they live here, where the cloud layer's tests run with it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from igni_core.core.db import schema
from igni_core.core.db.database import Database
from igni_core.core.db.locator import StorageLocator
from igni_core.core.db.schema import SchemaKeeper, SchemaRevision
from igni_core.core.db.scope import StorageScope, Tenant, tenancy
from igni_core.core.db.upsert import ConflictInsert
from igni_core.core.db.urls import DatabaseUrl

SERVER = "postgresql://igni:secret@db.internal:5432/igni"
UNREACHABLE = "postgresql://igni:pw@127.0.0.1:1/igni"

_table = Table("kv", MetaData(), Column("k", String, primary_key=True), Column("v", Integer))


@pytest.fixture
def migrator(monkeypatch: pytest.MonkeyPatch):
    """The module the keeper imports when a database is behind, as a recorder."""
    upgraded: list[DatabaseUrl] = []

    def upgrade_to_head(url: DatabaseUrl) -> None:
        upgraded.append(url)

    monkeypatch.setitem(
        sys.modules, "igni_test_migrations", SimpleNamespace(upgrade_to_head=upgrade_to_head)
    )
    monkeypatch.setattr(schema, "MIGRATIONS_MODULE", "igni_test_migrations")
    return SimpleNamespace(upgraded=upgraded)


def test_a_server_has_no_file():
    assert Database(SERVER, run_migrations=False).path is None


def test_the_global_database_is_the_producers_with_no_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # No server is listening: only which database and whose rows is asked here.
    monkeypatch.setattr(
        "igni_core.core.db.database.keeper", SimpleNamespace(ensure_current=lambda url: None)
    )
    locator = StorageLocator(database_url=SERVER, data_dir=tmp_path)
    tenancy.use(lambda: Tenant(org="acme", producer="app:7"))
    try:
        database = locator.global_database()
        assert database.scope == StorageScope(org="acme", producer="app:7", project="", shared=True)
        assert database.url == DatabaseUrl.parse(SERVER)
    finally:
        tenancy.use(None)


def test_a_sync_postgres_session_gets_the_postgres_insert():
    engine = create_engine("postgresql+psycopg://u:p@localhost/db")
    with Session(engine) as session:
        stmt = ConflictInsert.into(session, _table)
    assert isinstance(stmt, postgresql.Insert)


def test_a_server_that_cannot_be_reached_has_no_revision():
    assert SchemaRevision.of(UNREACHABLE) is None
    assert not SchemaRevision.is_current(UNREACHABLE)


def test_an_unreadable_server_is_handed_to_the_migrator(migrator: SimpleNamespace):
    SchemaKeeper().ensure_current(UNREACHABLE)
    assert migrator.upgraded == [DatabaseUrl.parse(UNREACHABLE)]
