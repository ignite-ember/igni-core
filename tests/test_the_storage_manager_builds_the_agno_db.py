"""``StorageManager`` builds the Agno db a session persists to: SQLite on
the desktop, the scoped shared database in the cloud. Nothing here stubs
Agno — what is built is what a session would use."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from agno.db.sqlite import AsyncSqliteDb
from agno.memory import MemoryManager

from igni_core.core.config.settings import Settings
from igni_core.core.data_paths import state_db_path
from igni_core.core.db.agno_schema import AgnoSchema
from igni_core.core.db.scope import NoTenantError, Tenant, tenancy
from igni_core.core.db.urls import DatabaseUrl
from igni_core.core.memory import StorageManager as ExportedStorageManager
from igni_core.core.memory.manager import StorageManager


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    settings = Settings()
    settings.storage.data_dir = str(tmp_path / "data")
    return settings


def test_the_package_exports_the_manager():
    assert ExportedStorageManager is StorageManager


def test_the_desktop_db_is_the_projects_state_file_with_igni_s_table_names(
    settings: Settings, tmp_path: Path
):
    project = tmp_path / "project"

    db = StorageManager(settings, project_dir=project).create_db()

    assert isinstance(db, AsyncSqliteDb)
    assert db.db_file == str(state_db_path(project, data_dir=settings.storage.data_dir))
    assert db.session_table_name == "ember_sessions"
    assert db.memory_table_name == "ember_memories"


def test_the_projects_data_directories_are_made_before_the_db_is_opened(
    settings: Settings, tmp_path: Path
):
    project = tmp_path / "some" / "deep" / "project"
    state = state_db_path(project, data_dir=settings.storage.data_dir)
    assert not state.parent.exists()

    StorageManager(settings, project_dir=project).create_db()

    assert state.parent.is_dir()


def test_a_project_dir_given_as_text_is_the_same_project_as_a_path(
    settings: Settings, tmp_path: Path
):
    as_text = StorageManager(settings, project_dir=str(tmp_path)).create_db()
    as_path = StorageManager(settings, project_dir=tmp_path).create_db()

    assert as_text.db_file == as_path.db_file


def test_without_a_project_dir_the_current_directory_is_the_project(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path / "here")

    manager = StorageManager(settings)

    assert manager.project_dir == tmp_path / "here"
    assert manager.create_db().db_file == str(
        state_db_path(tmp_path / "here", data_dir=settings.storage.data_dir)
    )


def test_an_existing_state_file_is_brought_to_the_current_schema_when_opened(
    settings: Settings, tmp_path: Path
):
    project = tmp_path / "project"
    state = state_db_path(project, data_dir=settings.storage.data_dir)
    state.parent.mkdir(parents=True)
    sqlite3.connect(state).close()
    AgnoSchema._current.discard(str(state.resolve()))

    StorageManager(settings, project_dir=project).create_db()

    assert str(state.resolve()) in AgnoSchema._current


def test_a_new_state_file_is_left_for_agno_to_create(settings: Settings, tmp_path: Path):
    project = tmp_path / "project"
    state = state_db_path(project, data_dir=settings.storage.data_dir)

    StorageManager(settings, project_dir=project).create_db()

    assert not state.exists()
    assert str(state.resolve()) not in AgnoSchema._current


def test_a_shared_database_is_used_with_no_local_state_made(
    settings: Settings, tmp_path: Path, database_url: DatabaseUrl
):
    settings.storage.database_url = database_url.url
    tenancy.use(lambda: Tenant(org="acme", producer="person:ada"))
    project = tmp_path / "project"

    db = StorageManager(settings, project_dir=project).create_db()

    assert db.session_table_name == "ember_sessions"
    assert db.memory_table_name == "ember_memories"
    assert not (tmp_path / "data").exists()
    assert not project.exists()


def test_a_shared_database_nobody_is_signed_in_to_is_refused(
    settings: Settings, tmp_path: Path, database_url: DatabaseUrl
):
    settings.storage.database_url = database_url.url
    tenancy.use(lambda: None)

    with pytest.raises(NoTenantError):
        StorageManager(settings, project_dir=tmp_path).create_db()


def test_an_unsupported_database_is_refused_not_replaced_by_sqlite(
    settings: Settings, tmp_path: Path
):
    settings.storage.database_url = "mysql://nobody@localhost/none"

    with pytest.raises(Exception):  # noqa: B017 — whichever error names the scheme
        StorageManager(settings, project_dir=tmp_path).create_db()

    assert not (tmp_path / "data").exists()


def test_the_memory_manager_is_bound_to_a_db_on_the_same_project(
    settings: Settings, tmp_path: Path
):
    memory = StorageManager.build_memory(settings, project_dir=tmp_path)

    assert isinstance(memory, MemoryManager)
    assert memory.db.db_file == str(state_db_path(tmp_path, data_dir=settings.storage.data_dir))


def test_build_db_is_a_manager_and_its_db_in_one_call(settings: Settings, tmp_path: Path):
    built = StorageManager.build_db(settings, project_dir=tmp_path)
    created = StorageManager(settings, project_dir=tmp_path).create_db()

    assert type(built) is type(created)
    assert built.db_file == created.db_file
