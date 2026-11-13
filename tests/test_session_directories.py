"""Tests for the global session → project-directory registry."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from igni_core.core.data_paths import global_db_path, resolve_project_id
from igni_core.core.db.database import Database
from igni_core.core.db.locator import StorageLocator
from igni_core.core.db.scope import StorageScope, Tenant
from igni_core.core.session.session_directories import (
    SessionDirectoryModel,
    SessionDirectoryStore,
)


def test_set_then_get(tmp_path):
    store = SessionDirectoryStore(tmp_path / "sessions.db")
    store.set_dir("sess-a", "/repos/alpha")
    assert store.get_dir("sess-a") == "/repos/alpha"


def test_unknown_session_returns_none(tmp_path):
    store = SessionDirectoryStore(tmp_path / "sessions.db")
    assert store.get_dir("nope") is None


def test_upsert_overwrites(tmp_path):
    store = SessionDirectoryStore(tmp_path / "sessions.db")
    store.set_dir("sess", "/old")
    store.set_dir("sess", "/new")
    assert store.get_dir("sess") == "/new"


def test_empty_session_id_is_noop(tmp_path):
    store = SessionDirectoryStore(tmp_path / "sessions.db")
    store.set_dir("", "/somewhere")
    assert store.get_dir("") is None


def test_survives_reopen(tmp_path):
    db = tmp_path / "sessions.db"
    SessionDirectoryStore(db).set_dir("sess", "/repos/alpha")
    assert SessionDirectoryStore(db).get_dir("sess") == "/repos/alpha"


def test_path_objects_stored_as_strings(tmp_path):
    store = SessionDirectoryStore(tmp_path / "sessions.db")
    store.set_dir("sess", tmp_path)
    assert store.get_dir("sess") == str(tmp_path)


def _broken(store: SessionDirectoryStore):
    def refuse():
        raise OperationalError("select 1", {}, RuntimeError("database is locked"))

    store._db.sync_session = refuse  # type: ignore[method-assign]


def test_a_failed_write_is_swallowed_and_records_nothing(tmp_path):
    db = tmp_path / "sessions.db"
    store = SessionDirectoryStore(db)
    _broken(store)

    store.set_dir("sess", "/repos/alpha")  # must not raise

    assert SessionDirectoryStore(db).get_dir("sess") is None


def test_a_failed_read_answers_none(tmp_path):
    db = tmp_path / "sessions.db"
    SessionDirectoryStore(db).set_dir("sess", "/repos/alpha")
    store = SessionDirectoryStore(db)
    _broken(store)

    assert store.get_dir("sess") is None


def test_the_locator_opens_the_global_database(tmp_path):
    locator = StorageLocator(database_url=None, data_dir=tmp_path)
    SessionDirectoryStore.from_locator(locator).set_dir("sess", "/repos/alpha")

    assert global_db_path(tmp_path).exists()
    assert SessionDirectoryStore.from_locator(locator).get_dir("sess") == "/repos/alpha"
    assert SessionDirectoryStore(global_db_path(tmp_path)).get_dir("sess") == "/repos/alpha"


def test_moving_a_session_to_another_project_updates_the_recorded_project(tmp_path):
    """Where producers share a database the row names the project; a later
    write for the same session replaces it rather than adding a second row."""
    scope = StorageScope.for_tenant(Tenant(org="acme", producer="person:ada"))
    db = Database(tmp_path / "shared.db", scope=scope)
    store = SessionDirectoryStore(db)
    first, second = tmp_path / "alpha", tmp_path / "beta"
    first.mkdir()
    second.mkdir()

    store.set_dir("sess", first)
    store.set_dir("sess", second)

    with db.sync_session() as session:
        rows = session.execute(
            select(SessionDirectoryModel.project, SessionDirectoryModel.project_dir)
        ).all()
    assert rows == [(resolve_project_id(second), str(second))]
