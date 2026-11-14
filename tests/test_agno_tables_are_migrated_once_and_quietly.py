"""Agno's tables in a SQLite file are brought to Agno 3 once per process, and quietly.

:class:`igni_core.core.db.agno_schema.AgnoSchema` runs Agno's own migration
on an existing file before any session is read, on a thread of its own so a
caller inside a running loop is not in its way. The file Agno 2.6 wrote is
``tests/fixtures/agno_2_6_state.db``; its tables are renamed here to the
names these tests pass in.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest
from agno.db.migrations.manager import MigrationManager
from agno.db.sqlite import AsyncSqliteDb

from igni_core.core.db.agno_schema import AgnoSchema

AGNO_2_6_DB = Path(__file__).parent / "fixtures" / "agno_2_6_state.db"
SESSIONS = "igni_sessions"
MEMORIES = "igni_memories"


def _write_agno_2_6_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(AGNO_2_6_DB.read_bytes())
    with sqlite3.connect(path) as connection:
        old = connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%\\_sessions' ESCAPE '\\'"
        )
        (old_name,) = old.fetchone()
        connection.execute(f"ALTER TABLE {old_name} RENAME TO {SESSIONS}")
        connection.execute(
            "UPDATE agno_schema_versions SET table_name = ? WHERE table_name = ?",
            (SESSIONS, old_name),
        )
    return path


def _runs_in_row(path: Path) -> list[object]:
    with sqlite3.connect(path) as connection:
        return [row[0] for row in connection.execute(f"SELECT runs FROM {SESSIONS}")]  # noqa: S608


def _version(path: Path) -> str | None:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT version FROM agno_schema_versions WHERE table_name = ?", (SESSIONS,)
        ).fetchone()
    return row[0] if row else None


def _ensure(path: Path) -> None:
    AgnoSchema.ensure_current(path, session_table=SESSIONS, memory_table=MEMORIES)


@pytest.fixture(autouse=True)
def _fresh_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts as a process that has migrated nothing."""
    monkeypatch.setattr(AgnoSchema, "_current", set())


@pytest.fixture
def agno_2_6_file(tmp_path: Path) -> Path:
    return _write_agno_2_6_file(tmp_path / "state.db")


def test_a_file_that_does_not_exist_is_left_for_agno_to_create(tmp_path: Path):
    path = tmp_path / "new" / "state.db"
    _ensure(path)
    assert not path.exists()
    assert not path.parent.exists()
    assert AgnoSchema._current == set()


def test_an_existing_file_is_migrated_and_its_old_runs_are_retired(agno_2_6_file: Path):
    assert _version(agno_2_6_file) == "2.5.6"
    assert _runs_in_row(agno_2_6_file) != [None]
    _ensure(agno_2_6_file)
    assert _version(agno_2_6_file) == "3.0.0"
    assert _runs_in_row(agno_2_6_file) == [None]


async def test_a_caller_inside_a_running_loop_waits_for_the_migration(agno_2_6_file: Path):
    _ensure(agno_2_6_file)
    assert _version(agno_2_6_file) == "3.0.0"


def test_a_second_call_on_one_file_does_not_migrate_again(tmp_path: Path):
    """Under another spelling of the same path too: the second call is a no-op."""
    path = _write_agno_2_6_file(tmp_path / "a" / "state.db")
    _ensure(path)
    # Put the old file back: only a migration run would bring it to 3.0.0 again.
    _write_agno_2_6_file(path)
    _ensure(path)
    _ensure(tmp_path / "a" / ".." / "a" / "state.db")
    assert _version(path) == "2.5.6"


def test_a_failed_migration_reaches_the_caller_and_is_retried_next_time(tmp_path: Path):
    path = tmp_path / "state.db"
    path.write_bytes(b"this is not a database, and never was one" * 20)
    with pytest.raises(Exception):  # noqa: B017, PT011 — whatever the file makes SQLite say
        _ensure(path)
    assert AgnoSchema._current == set()
    _write_agno_2_6_file(path)
    _ensure(path)
    assert _version(path) == "3.0.0"


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.levels: list[int] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.levels.append(record.levelno)


@pytest.fixture
def agno_log():
    log = logging.getLogger("agno")
    saved_level, saved_handlers = log.level, list(log.handlers)
    log.setLevel(logging.INFO)
    records = _Records()
    log.addHandler(records)
    yield log, records
    log.removeHandler(records)
    log.setLevel(saved_level)
    assert list(log.handlers) == saved_handlers


def test_agno_does_not_narrate_the_migration_and_its_level_comes_back(
    agno_2_6_file: Path, agno_log
):
    log, records = agno_log
    _ensure(agno_2_6_file)
    assert logging.INFO not in records.levels
    assert log.level == logging.INFO


def test_agno_s_level_comes_back_when_the_migration_fails(tmp_path: Path, agno_log):
    log, _ = agno_log
    path = tmp_path / "state.db"
    path.write_bytes(b"this is not a database, and never was one" * 20)
    with pytest.raises(Exception):  # noqa: B017, PT011
        _ensure(path)
    assert log.level == logging.INFO


def test_a_louder_agno_level_is_not_lowered_for_the_migration(
    agno_2_6_file: Path, agno_log, monkeypatch: pytest.MonkeyPatch
):
    log, _ = agno_log
    log.setLevel(logging.ERROR)
    during: list[int] = []
    real_up = MigrationManager.up

    async def watching_up(self, *args, **kwargs):
        during.append(log.level)
        return await real_up(self, *args, **kwargs)

    monkeypatch.setattr(MigrationManager, "up", watching_up)
    _ensure(agno_2_6_file)
    assert during and set(during) == {logging.ERROR}
    assert log.level == logging.ERROR


async def test_the_old_runs_stay_where_an_early_schema_keeps_them(agno_2_6_file: Path):
    db = AsyncSqliteDb(db_file=str(agno_2_6_file), session_table=SESSIONS, memory_table=MEMORIES)
    try:
        await AgnoSchema.retire_legacy_runs(db, SESSIONS)
    finally:
        await db.db_engine.dispose()
    assert _runs_in_row(agno_2_6_file) != [None]


async def test_a_session_table_that_has_no_version_keeps_its_runs(agno_2_6_file: Path):
    with sqlite3.connect(agno_2_6_file) as connection:
        connection.execute("DELETE FROM agno_schema_versions")
    db = AsyncSqliteDb(db_file=str(agno_2_6_file), session_table=SESSIONS, memory_table=MEMORIES)
    try:
        await AgnoSchema.retire_legacy_runs(db, SESSIONS)
    finally:
        await db.db_engine.dispose()
    assert _runs_in_row(agno_2_6_file) != [None]


async def test_a_migrated_table_without_the_runs_column_is_left_alone(agno_2_6_file: Path):
    _ensure(agno_2_6_file)
    with sqlite3.connect(agno_2_6_file) as connection:
        connection.execute(f"ALTER TABLE {SESSIONS} DROP COLUMN runs")
    db = AsyncSqliteDb(db_file=str(agno_2_6_file), session_table=SESSIONS, memory_table=MEMORIES)
    try:
        await AgnoSchema.retire_legacy_runs(db, SESSIONS)
    finally:
        await db.db_engine.dispose()
    with sqlite3.connect(agno_2_6_file) as connection:
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({SESSIONS})")]
    assert "runs" not in columns
