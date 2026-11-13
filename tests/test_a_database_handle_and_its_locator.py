"""A :class:`Database` names one database; a :class:`StorageLocator` chooses which.

The handle brings a file's schema to head on construction unless told not
to, shares one pool per database through the process's registry, and moves
between scopes without migrating. The locator turns the storage settings
into handles: files under the data directory on the desktop, the one
configured database in the cloud.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from igni_core.core.data_paths import global_db_path, resolve_project_id, state_db_path
from igni_core.core.db.database import Database
from igni_core.core.db.locator import StorageLocator
from igni_core.core.db.schema import HEAD_REVISION, SchemaRevision
from igni_core.core.db.scope import NoTenantError, StorageScope, Tenant, tenancy
from igni_core.core.db.urls import DatabaseUrl
from igni_core.core.layer import Layer, layers
from igni_core.core.paths import DEFAULT_DATA_DIR

SERVER = "postgresql://igni:secret@db.internal:5432/igni"


class SharedDatabaseLayer(Layer):
    name = "shared-test"

    def requires_database(self) -> bool:
        return True


# ── Database ─────────────────────────────────────────────────────────


def test_a_new_handle_makes_the_file_and_brings_it_to_head(tmp_path: Path):
    path = tmp_path / "deep" / "state.db"
    database = Database(path)
    assert database.path == path.resolve()
    assert SchemaRevision.of(path) == HEAD_REVISION
    assert database.scope == StorageScope.local()


def test_a_handle_told_not_to_migrate_leaves_the_file_alone(tmp_path: Path):
    path = tmp_path / "state.db"
    Database(path, run_migrations=False)
    assert not path.exists()
    assert SchemaRevision.of(path) is None


def test_a_handle_in_another_scope_does_not_migrate(tmp_path: Path):
    path = tmp_path / "state.db"
    original = Database(path, run_migrations=False)
    other = original.in_scope(StorageScope(org="o", producer="p", project="x", shared=True))
    assert not path.exists()
    assert other.url == original.url
    assert other.scope.org == "o"
    assert original.scope == StorageScope.local()


def test_of_hands_back_a_handle_and_builds_one_from_a_path(tmp_path: Path):
    handle = Database(tmp_path / "a.db")
    assert Database.of(handle) is handle
    built = Database.of(tmp_path / "b.db")
    assert isinstance(built, Database)
    assert built.path == (tmp_path / "b.db").resolve()
    from_url = Database.of(f"sqlite:///{tmp_path / 'c.db'}")
    assert from_url.path == (tmp_path / "c.db").resolve()


def test_two_handles_on_one_file_share_their_pool_and_sessions_keep_what_they_committed(
    tmp_path: Path,
):
    path = tmp_path / "state.db"
    first, second = Database(path), Database(tmp_path / "." / "state.db")
    assert first._sessionmaker is second._sessionmaker
    assert first._sync_sessionmaker is second._sync_sessionmaker
    assert first.session().sync_session.bind is second.session().sync_session.bind
    assert first.sync_session().expire_on_commit is False
    assert first.session().sync_session.expire_on_commit is False


def test_an_in_memory_database_or_another_dialect_is_refused():
    with pytest.raises(ValueError, match="in-memory"):
        Database("sqlite:///:memory:")
    with pytest.raises(ValueError, match="mysql"):
        Database("mysql://u:p@h/db")


# ── StorageLocator ───────────────────────────────────────────────────


def test_a_layer_that_keeps_state_in_a_database_refuses_to_start_without_one(tmp_path: Path):
    layers.install(SharedDatabaseLayer())
    with pytest.raises(RuntimeError, match="IGNI_DATABASE_URL") as refusal:
        StorageLocator(database_url=None, data_dir=tmp_path)
    assert "shared-test" in str(refusal.value)
    with pytest.raises(RuntimeError, match="IGNI_DATABASE_URL"):
        StorageLocator(database_url="", data_dir=tmp_path)
    assert StorageLocator(database_url=SERVER, data_dir=tmp_path).shared


def test_the_desktop_layer_starts_without_a_database_url(tmp_path: Path):
    assert StorageLocator(database_url=None, data_dir=tmp_path).shared is False


def test_a_malformed_database_url_is_refused_at_construction(tmp_path: Path):
    with pytest.raises(ValueError, match="mysql"):
        StorageLocator(database_url="mysql://u:p@h/db", data_dir=tmp_path)


@pytest.mark.parametrize("failure", [ImportError, FileNotFoundError])
def test_settings_that_cannot_be_read_fall_back_to_the_defaults(
    failure: type[Exception], monkeypatch: pytest.MonkeyPatch
):
    def unreadable():
        raise failure("no settings here")

    monkeypatch.setattr("igni_core.core.db.locator.Settings", unreadable)
    locator = StorageLocator.current()
    assert locator.shared is False
    assert locator.global_url() == DatabaseUrl.for_file(global_db_path(DEFAULT_DATA_DIR))


def test_settings_that_do_not_validate_fall_back_to_the_defaults(
    monkeypatch: pytest.MonkeyPatch,
):
    def invalid():
        from igni_core.core.config.schemas.storage_and_paths import StorageConfig

        StorageConfig(database_url=5)  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        invalid()
    monkeypatch.setattr("igni_core.core.db.locator.Settings", invalid)
    locator = StorageLocator.current()
    assert locator.shared is False
    assert locator.global_url() == DatabaseUrl.for_file(global_db_path(DEFAULT_DATA_DIR))


def test_the_desktop_keeps_a_project_and_the_global_state_in_files(tmp_path: Path):
    locator = StorageLocator(database_url=None, data_dir=tmp_path / "data")
    project = tmp_path / "work"
    project.mkdir()
    assert locator.project_url(project) == DatabaseUrl.for_file(
        state_db_path(project, data_dir=tmp_path / "data")
    )
    assert locator.global_url() == DatabaseUrl.for_file(global_db_path(tmp_path / "data"))
    assert locator.project_url(str(project)) == locator.project_url(project)
    assert locator.scope(project) == StorageScope.local()


def test_the_cloud_keeps_everything_in_the_one_database(tmp_path: Path):
    locator = StorageLocator(database_url=SERVER, data_dir=tmp_path)
    assert locator.shared is True
    assert locator.project_url(tmp_path) == DatabaseUrl.parse(SERVER)
    assert locator.global_url() == DatabaseUrl.parse(SERVER)


def test_the_cloud_scope_is_the_signed_in_producers_narrowed_to_the_folder(tmp_path: Path):
    locator = StorageLocator(database_url=SERVER, data_dir=tmp_path)
    tenancy.use(lambda: Tenant(org="acme", producer="person:1"))
    folder = tmp_path / "work"
    folder.mkdir()
    scope = locator.scope(folder)
    assert (scope.org, scope.producer, scope.shared) == ("acme", "person:1", True)
    assert scope.project == resolve_project_id(folder)
    assert locator.scope().project == ""
    tenancy.use(lambda: Tenant(org="acme", producer="person:2"))
    assert locator.scope(folder).producer == "person:2"


def test_the_cloud_refuses_to_open_a_database_with_nobody_signed_in(tmp_path: Path):
    locator = StorageLocator(database_url=SERVER, data_dir=tmp_path)
    with pytest.raises(NoTenantError):
        locator.scope(tmp_path)
    with pytest.raises(NoTenantError):
        locator.global_database()


def test_a_project_database_defaults_to_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    data = tmp_path / "data"
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    database = StorageLocator(database_url=None, data_dir=data).project_database()
    assert database.path == state_db_path(Path.cwd(), data_dir=data).resolve()
    assert SchemaRevision.of(database.url) == HEAD_REVISION


def test_the_global_database_on_the_desktop_is_the_igni_db_file(tmp_path: Path):
    database = StorageLocator(database_url=None, data_dir=tmp_path).global_database()
    assert database.path == global_db_path(tmp_path).resolve()
    assert database.scope == StorageScope.local()


def test_an_explicit_database_path_wins_over_the_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def settings_must_not_be_read():
        raise AssertionError("an explicit path never asks the settings")

    monkeypatch.setattr(
        StorageLocator, "current", classmethod(lambda cls: settings_must_not_be_read())
    )
    chosen = tmp_path / "chosen.db"
    database = StorageLocator.database_for(chosen, tmp_path / "work")
    assert database.path == chosen.resolve()
    assert database.scope == StorageScope.local()


def test_without_a_path_a_store_gets_the_current_settings_project_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    data = tmp_path / "data"
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(
        StorageLocator,
        "current",
        classmethod(lambda cls: StorageLocator(database_url=None, data_dir=data)),
    )
    database = StorageLocator.database_for(None, work)
    assert database.path == state_db_path(work, data_dir=data).resolve()
