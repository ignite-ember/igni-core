# ruff: noqa: S608  (the SQL names fixed tables from the migration, never input)
"""What the two revisions that scope every table and persist approvals do.

``e3b9d4a1c6f2`` drops and recreates the alembic tables with org, producer
and project in their keys, adopts the tables the raw stores made for
themselves, and adds memory files and attachments. ``f7c2a8e4b1d3`` adds
the waiting approvals and the queued messages. Each test migrates a real
sqlite file to exactly the revision it is about, so a later revision
cannot mask what these two define.
"""

from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

import igni_core

PACKAGE = Path(igni_core.__file__).resolve().parent
BEFORE_SCOPE = "c7f1a9e2d8b4"
SCOPE = "e3b9d4a1c6f2"
APPROVALS = "f7c2a8e4b1d3"

_scope_module = importlib.import_module(
    "igni_core.migrations.versions.e3b9d4a1c6f2_scope_every_table"
)
RAW_STORE_TABLES = _scope_module._UNMANAGED_TABLES
(RECEIVED, PREFERENCES, DIRECTORIES, CLIENT_STATE, NOTIFICATIONS) = RAW_STORE_TABLES


def _config(db: Path) -> Config:
    config = Config(str(PACKAGE / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    config.attributes["configure_logging"] = False
    return config


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


def _engine(db: Path) -> sa.Engine:
    return sa.create_engine(f"sqlite:///{db}")


def _inspect(db: Path):
    return sa.inspect(_engine(db))


def _columns(db: Path, table: str) -> dict[str, dict]:
    return {c["name"]: c for c in _inspect(db).get_columns(table)}


def _tables(db: Path) -> set[str]:
    return set(_inspect(db).get_table_names()) - {"alembic_version"}


def _index_names(db: Path, table: str) -> set[str]:
    return {i["name"] for i in _inspect(db).get_indexes(table)}


def _insert_refused(db: Path, sql: str, *params) -> bool:
    with sqlite3.connect(db) as conn:
        try:
            conn.execute(sql, params)
        except sqlite3.IntegrityError:
            return True
    return False


NOW = "2026-01-01 00:00:00"


def test_scoping_drops_the_old_rows_and_the_raw_store_tables_and_recreates_them_empty(db: Path):
    command.upgrade(_config(db), BEFORE_SCOPE)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "insert into scheduler_tasks (id, description, scheduled_at, created_at)"
            f" values ('t1', 'd', '{NOW}', '{NOW}')"
        )
        conn.execute("insert into background_processes (pid, cmd, started_at) values (7, 'x', 1)")
        for table in RAW_STORE_TABLES:
            conn.execute(f"create table {table} (legacy text)")
            conn.execute(f"insert into {table} values ('old')")

    command.upgrade(_config(db), SCOPE)

    with sqlite3.connect(db) as conn:
        for table in ("scheduler_tasks", "background_processes", *RAW_STORE_TABLES):
            assert conn.execute(f"select count(*) from {table}").fetchone() == (0,), table
    for table in RAW_STORE_TABLES:
        assert "legacy" not in _columns(db, table), table
    assert "message_id" in _columns(db, RECEIVED)


def test_scoping_a_fresh_database_walked_from_the_base_has_every_scoped_table(db: Path):
    command.upgrade(_config(db), SCOPE)
    tables = _tables(db)
    assert {*RAW_STORE_TABLES, "memory_files", "attachments"} <= tables
    for table in (
        "scheduler_tasks",
        "loop_state",
        "loop_progress",
        "background_processes",
        *RAW_STORE_TABLES,
        "memory_files",
        "attachments",
    ):
        for column in ("org", "producer", "project"):
            info = _columns(db, table)[column]
            assert info["nullable"] is False, (table, column)
            assert "''" in str(info["default"]), (table, column)


def test_a_row_that_omits_its_scope_holds_the_empty_desktop_scope(db: Path):
    command.upgrade(_config(db), SCOPE)
    with sqlite3.connect(db) as conn:
        conn.execute("insert into background_processes (pid, cmd, started_at) values (1, 'x', 1)")
        row = conn.execute("select org, producer, project from background_processes").fetchone()
    assert row == ("", "", "")


def test_loop_state_holds_only_id_one_and_one_row_per_scope(db: Path):
    command.upgrade(_config(db), SCOPE)
    insert = (
        "insert into loop_state (org, producer, project, id, run_id, prompt, iteration_index,"
        f" iterations_remaining, created_at, updated_at) values (?, 'p', 'j', ?, 'r', 'go', 0, 3, '{NOW}', '{NOW}')"
    )
    assert _insert_refused(db, insert, "o", 2)
    assert not _insert_refused(db, insert, "o", 1)
    assert _insert_refused(db, insert, "o", 1)
    assert not _insert_refused(db, insert, "other", 1)


def test_progress_notes_and_processes_are_unique_per_scope_not_across_scopes(db: Path):
    command.upgrade(_config(db), SCOPE)
    progress = (
        "insert into loop_progress (org, producer, project, run_id, key, created_at, updated_at)"
        f" values (?, '', '', 'r', 'k', '{NOW}', '{NOW}')"
    )
    assert not _insert_refused(db, progress, "a")
    assert _insert_refused(db, progress, "a")
    assert not _insert_refused(db, progress, "b")

    process = "insert into background_processes (org, pid, cmd, started_at) values (?, 5, 'x', 1)"
    assert not _insert_refused(db, process, "a")
    assert _insert_refused(db, process, "a")
    assert not _insert_refused(db, process, "b")

    notification = (
        f"insert into {NOTIFICATIONS} (id, org, source, kind, title, body, target, event_key, created_at)"
        " values (?, ?, 's', 'k', 't', 'b', '', 'e', 'c')"
    )
    assert not _insert_refused(db, notification, "n1", "a")
    assert _insert_refused(db, notification, "n2", "a")
    assert not _insert_refused(db, notification, "n3", "b")

    memory = (
        "insert into memory_files (org, name, content, updated_at) values (?, 'MEMORY.md', 'c', 1)"
    )
    assert not _insert_refused(db, memory, "a")
    assert _insert_refused(db, memory, "a")
    assert not _insert_refused(db, memory, "b")


def test_a_session_directory_is_found_without_its_project_so_two_projects_cannot_share_one(
    db: Path,
):
    command.upgrade(_config(db), SCOPE)
    insert = (
        f"insert into {DIRECTORIES} (org, producer, project, session_id, project_dir, updated_at)"
        " values ('o', 'p', ?, 's1', '/d', 1)"
    )
    assert not _insert_refused(db, insert, "one")
    assert _insert_refused(db, insert, "two")
    other_org = insert.replace("'o', 'p'", "'o2', 'p'")
    assert not _insert_refused(db, other_org, "two")


def test_preferences_and_client_state_keep_a_project_in_their_key(db: Path):
    command.upgrade(_config(db), SCOPE)
    preference = f"insert into {PREFERENCES} (project, session_id, updated_at) values (?, 's', 1)"
    assert not _insert_refused(db, preference, "one")
    assert not _insert_refused(db, preference, "two")
    assert _insert_refused(db, preference, "one")
    state = f"insert into {CLIENT_STATE} (project, client_id, key, value, updated_at) values (?, 'c', 'k', 'v', 1)"
    assert not _insert_refused(db, state, "one")
    assert not _insert_refused(db, state, "two")
    assert _insert_refused(db, state, "one")


def test_message_ids_and_attachment_ids_are_global_across_scopes(db: Path):
    command.upgrade(_config(db), SCOPE)
    message = (
        f"insert into {RECEIVED} (org, message_id, session_id, text, received_at)"
        " values (?, 'm1', 's', 't', 1)"
    )
    assert not _insert_refused(db, message, "a")
    assert _insert_refused(db, message, "b")
    attachment = (
        "insert into attachments (org, id, session_id, filename, oid, size, created_at)"
        " values (?, 'a1', 's', 'f.txt', 9999999999, 5000000000, 1)"
    )
    assert not _insert_refused(db, attachment, "a")
    assert _insert_refused(db, attachment, "b")
    with sqlite3.connect(db) as conn:
        assert conn.execute("select oid, size from attachments").fetchone() == (
            9999999999,
            5000000000,
        )


def test_the_scoped_tables_carry_their_indexes(db: Path):
    command.upgrade(_config(db), SCOPE)
    assert "ix_scheduler_tasks_scope" in _index_names(db, "scheduler_tasks")
    assert "ix_loop_progress_run_id" in _index_names(db, "loop_progress")
    assert f"ix_{RECEIVED}_session_status" in _index_names(db, RECEIVED)
    assert "ix_local_notifications_scope_created" in _index_names(db, NOTIFICATIONS)
    assert "ix_attachments_scope_session" in _index_names(db, "attachments")


def test_scoping_downgrades_to_the_empty_unscoped_tables_and_upgrades_again(db: Path):
    config = _config(db)
    command.upgrade(config, SCOPE)
    command.downgrade(config, BEFORE_SCOPE)

    tables = _tables(db)
    assert tables.isdisjoint({*RAW_STORE_TABLES, "memory_files", "attachments"})
    for table in ("scheduler_tasks", "loop_state", "loop_progress", "background_processes"):
        assert "org" not in _columns(db, table), table
    assert "ix_loop_progress_run_id" in _index_names(db, "loop_progress")
    with sqlite3.connect(db) as conn:
        for table in ("scheduler_tasks", "loop_state", "loop_progress", "background_processes"):
            assert conn.execute(f"select count(*) from {table}").fetchone() == (0,)

    command.upgrade(config, SCOPE)
    assert "org" in _columns(db, "loop_state")
    assert "memory_files" in _tables(db)


def test_the_approvals_revision_adds_only_its_two_tables_and_touches_no_data(db: Path):
    config = _config(db)
    command.upgrade(config, SCOPE)
    with sqlite3.connect(db) as conn:
        conn.execute("insert into background_processes (pid, cmd, started_at) values (1, 'x', 1)")
    before = _tables(db)

    command.upgrade(config, APPROVALS)

    assert _tables(db) - before == {"waiting_approvals", "queued_messages"}
    with sqlite3.connect(db) as conn:
        assert conn.execute("select pid from background_processes").fetchall() == [(1,)]


def test_a_waiting_approval_defaults_its_run_and_request_and_is_unique_within_a_scope(db: Path):
    command.upgrade(_config(db), APPROVALS)
    columns = _columns(db, "waiting_approvals")
    assert set(columns) == {
        "org",
        "producer",
        "project",
        "requirement_id",
        "session_id",
        "run_id",
        "origin",
        "requirement",
        "request",
        "created_at",
    }
    assert all(not c["nullable"] for c in columns.values())

    insert = (
        "insert into waiting_approvals (org, producer, requirement_id, session_id, origin,"
        " requirement, created_at) values (?, ?, 'req', 's', 'tool', ?, 1)"
    )
    big = '{"text": "' + "é" * 100_000 + '"}'
    assert not _insert_refused(db, insert, "o", "p", big)
    assert _insert_refused(db, insert, "o", "p", "{}")
    assert not _insert_refused(db, insert, "o", "other", "{}")
    assert not _insert_refused(db, insert, "other", "p", "{}")
    other_project = insert.replace("(org, producer,", "(org, producer, project,").replace(
        "values (?, ?,", "values (?, ?, 'j',"
    )
    assert not _insert_refused(db, other_project, "o", "p", "{}")
    assert _insert_refused(db, other_project, "o", "p", "{}")
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "select run_id, request, project, length(requirement) from waiting_approvals"
            " where org = 'o' and producer = 'p'"
        ).fetchone()
    assert row == ("", "", "", len(big))
    assert "ix_waiting_approvals_session" in _index_names(db, "waiting_approvals")


def test_queued_messages_are_ordered_by_seq_with_repeats_allowed_and_ids_global(db: Path):
    command.upgrade(_config(db), APPROVALS)
    columns = _columns(db, "queued_messages")
    assert set(columns) == {"org", "producer", "project", "id", "session_id", "seq", "text"}
    insert = (
        "insert into queued_messages (org, id, session_id, seq, text) values (?, ?, 's', ?, 'hej')"
    )
    assert not _insert_refused(db, insert, "a", "m1", 5_000_000_000)
    assert not _insert_refused(db, insert, "a", "m2", 5_000_000_000)
    assert _insert_refused(db, insert, "b", "m1", 1)
    index = next(
        i
        for i in _inspect(db).get_indexes("queued_messages")
        if i["name"] == "ix_queued_messages_session"
    )
    assert index["column_names"] == ["org", "producer", "project", "session_id", "seq"]
    assert not index["unique"]


def test_the_approvals_revision_downgrades_to_the_scoped_schema_and_upgrades_again(db: Path):
    config = _config(db)
    command.upgrade(config, APPROVALS)
    command.downgrade(config, SCOPE)
    assert {"waiting_approvals", "queued_messages"}.isdisjoint(_tables(db))
    assert "memory_files" in _tables(db)
    command.upgrade(config, APPROVALS)
    assert {"waiting_approvals", "queued_messages"} <= _tables(db)
