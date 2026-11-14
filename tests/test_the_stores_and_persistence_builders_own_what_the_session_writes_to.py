"""The stores and persistence builders: what a session writes into, and where.

The loop's state and the memory directory live in the project, the
event log persists through whatever persister the session holds when an
event is appended (none while the session is still being built), and the
persister, the memory manager and the learning manager are keyed by the
session's identity, which the learning manager reads on every use.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from igni_core.core.learn import LearnBootResult
from igni_core.core.session.parts import persistence as persistence_module
from igni_core.core.session.parts.persistence import PersistenceBuilder
from igni_core.core.session.parts.stores import StoresBuilder
from igni_core.core.utils.context_memory import ProjectMemoryBank
from tests.session_factory import build_session, stub_project


@pytest.fixture
def session(tmp_path: Path):
    return build_session(tmp_path)


def test_the_loop_controller_owns_the_loop_store_the_session_holds(session) -> None:
    assert session.loop._store is session.loop_store


def test_the_memory_directory_is_created_for_a_project_without_one(tmp_path: Path) -> None:
    project = stub_project(tmp_path / "proj")
    bare = SimpleNamespace(project_dir=project)
    assert not ProjectMemoryBank(project).igni_dir().exists()

    StoresBuilder(bare).build_scratch()

    assert ProjectMemoryBank(project).igni_dir().is_dir()


def test_there_is_no_memory_mirror_on_the_desktop(session) -> None:
    assert session.memory_mirror is None


def test_the_event_log_finds_no_persister_until_the_session_has_one(tmp_path: Path) -> None:
    project = stub_project(tmp_path / "proj")
    bare = SimpleNamespace(project_dir=project)
    StoresBuilder(bare).build_scratch()
    persist = bare.event_log_store._persist_ref

    assert persist() is None
    bare.persistence = "the persister"
    assert persist() == "the persister"


def test_the_scratch_stores_start_empty(session) -> None:
    assert session.reminders.pending == []
    assert session.plan_store.decisions == {}


def test_the_database_is_built_for_the_sessions_settings_and_project(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[tuple[object, object]] = []
    sentinel = object()

    def build_db(settings, project_dir=None):
        asked.append((settings, project_dir))
        return sentinel

    monkeypatch.setattr(persistence_module.StorageManager, "build_db", staticmethod(build_db))

    PersistenceBuilder(session).build_database()

    assert session.db is sentinel
    assert asked == [(session.settings, session.project_dir)]


def test_the_persister_and_memory_manager_are_keyed_by_the_session_identity(session) -> None:
    assert session.persistence.session_id == session.session_id
    assert session.memory_mgr.user_id == session.user_id
    assert session.memory_mgr.db is session.db


def test_the_learning_manager_reads_the_live_ids_and_team(session) -> None:
    session.rotate_id("rotated3")
    replaced = SimpleNamespace()
    session.main_team = replaced

    manager = session.learning_mgr

    assert manager._session_id_ref() == "rotated3"
    assert manager._user_id_ref() == session.user_id
    assert manager._main_team_ref() is replaced


def test_a_learning_machine_that_could_not_boot_says_why(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(
        persistence_module,
        "create_learning_machine",
        lambda settings, db: LearnBootResult(ok=False, machine=None, reason="no database today"),
    )
    monkeypatch.setattr(
        "igni_core.core.session.learning_ops.logger.warning",
        lambda msg, *args: warnings.append(msg % args),
    )

    PersistenceBuilder(session).build_persistence()

    assert session.learning_mgr.machine is None
    assert warnings == ["LearningMachine unavailable: no database today"]


def test_the_learning_manager_holds_the_machine_that_booted(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = object()
    monkeypatch.setattr(
        persistence_module,
        "create_learning_machine",
        lambda settings, db: LearnBootResult(ok=True, machine=machine, reason=""),
    )

    PersistenceBuilder(session).build_persistence()

    assert session.learning_mgr.machine is machine
