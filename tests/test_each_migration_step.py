"""Each revision of the migration chain, run on its own, on both dialects.

``test_storage_backends`` and the drift check pin the schema at head; they
do not see what one revision leaves behind, whether it refuses what it
promises to refuse, what it does to rows already there, or whether it can
be taken back. Here a real database is walked to a revision and used.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

import igni_core
from igni_core.core.db.urls import DatabaseUrl, Dialect
from tests.conftest import POSTGRES_TEST_URL_ENV
from tests.storage_backends import ThrowawayPostgres

PACKAGE = Path(igni_core.__file__).resolve().parent
NOW = datetime(2026, 1, 1)


class Chain:
    """One database and the alembic commands that move it along."""

    def __init__(self, url: DatabaseUrl):
        self.config = Config(str(PACKAGE / "alembic.ini"))
        self.config.set_main_option("sqlalchemy.url", url.sync.replace("%", "%%"))
        self.config.attributes["configure_logging"] = False
        self.engine = sa.create_engine(url.sync)

    def up(self, revision: str) -> None:
        command.upgrade(self.config, revision)

    def down(self, revision: str) -> None:
        command.downgrade(self.config, revision)

    def tables(self) -> set[str]:
        return set(sa.inspect(self.engine).get_table_names()) - {"alembic_version"}

    def columns(self, table: str) -> dict[str, dict]:
        return {c["name"]: c for c in sa.inspect(self.engine).get_columns(table)}

    def indexes(self, table: str) -> set[str]:
        return {i["name"] for i in sa.inspect(self.engine).get_indexes(table)}

    def run(self, sql: str, **params):
        with self.engine.begin() as conn:
            return conn.execute(sa.text(sql), params)

    def rows(self, sql: str, **params) -> list[tuple]:
        with self.engine.connect() as conn:
            return [tuple(r) for r in conn.execute(sa.text(sql), params)]


@pytest.fixture(params=[Dialect.SQLITE, Dialect.POSTGRESQL], ids=lambda d: d.value)
def chain(request, tmp_path: Path):
    """A database of the test's own, so no revision starts on another's tables."""
    throwaway = None
    if request.param is Dialect.POSTGRESQL:
        server = os.environ.get(POSTGRES_TEST_URL_ENV, "").strip()
        if not server:
            pytest.skip(f"{POSTGRES_TEST_URL_ENV} is unset")
        throwaway = ThrowawayPostgres(server)
        url = DatabaseUrl.parse(throwaway.create())
    else:
        url = DatabaseUrl.for_file(tmp_path / "state.db")
    walker = Chain(url)
    try:
        yield walker
    finally:
        walker.engine.dispose()
        if throwaway is not None:
            throwaway.drop()


def test_the_first_revision_creates_the_scheduler_and_code_index_tables_and_downgrades_to_nothing(
    chain: Chain,
):
    chain.up("9318ebdb0db5")
    assert chain.tables() == {
        "code_index_commit_metadata",
        "code_index_file_reference",
        "scheduler_tasks",
    }
    assert chain.indexes("code_index_commit_metadata") == {"idx_cicm_commit"}
    assert chain.indexes("code_index_file_reference") == {"idx_cifr_to", "idx_cifr_relation"}

    chain.down("base")
    assert chain.tables() == set()


def test_a_scheduler_task_inserted_with_only_its_required_columns_gets_the_server_defaults(
    chain: Chain,
):
    chain.up("9318ebdb0db5")
    chain.run(
        "INSERT INTO scheduler_tasks (id, description, scheduled_at, created_at)"
        " VALUES ('t', 'd', :at, :at)",
        at=NOW,
    )
    assert chain.rows("SELECT status, result, error, recurrence FROM scheduler_tasks") == [
        ("pending", "", "", "")
    ]


def test_the_code_index_json_columns_hold_json_on_the_first_revision(chain: Chain):
    chain.up("9318ebdb0db5")
    chain.run(
        "INSERT INTO code_index_commit_metadata (item_id, commit_sha, key, value)"
        " VALUES ('i', 'c', 'k', '{\"a\": 1}')"
    )
    with pytest.raises(IntegrityError):
        chain.run(
            "INSERT INTO code_index_commit_metadata (item_id, commit_sha, key, value)"
            " VALUES ('i', 'c', 'k', '{}')"
        )


def test_the_loop_revision_allows_one_loop_row_and_refuses_a_second_progress_key(chain: Chain):
    chain.up("4f7a1c2e9b3d")
    assert {"loop_state", "loop_progress"} <= chain.tables()
    assert chain.indexes("loop_progress") >= {"ix_loop_progress_run_id"}

    chain.run(
        "INSERT INTO loop_state (id, run_id, prompt, iteration_index, iterations_remaining,"
        " created_at, updated_at) VALUES (1, 'r', 'p', 0, 3, :at, :at)",
        at=NOW,
    )
    with pytest.raises(IntegrityError):
        chain.run(
            "INSERT INTO loop_state (id, run_id, prompt, iteration_index, iterations_remaining,"
            " created_at, updated_at) VALUES (2, 'r', 'p', 0, 3, :at, :at)",
            at=NOW,
        )

    chain.run(
        "INSERT INTO loop_progress (run_id, key, created_at, updated_at)"
        " VALUES ('r', 'k', :at, :at)",
        at=NOW,
    )
    assert chain.rows("SELECT value FROM loop_progress") == [("",)]
    chain.run(
        "INSERT INTO loop_progress (run_id, key, created_at, updated_at)"
        " VALUES ('other', 'k', :at, :at)",
        at=NOW,
    )
    with pytest.raises(IntegrityError):
        chain.run(
            "INSERT INTO loop_progress (run_id, key, created_at, updated_at)"
            " VALUES ('r', 'k', :at, :at)",
            at=NOW,
        )


def test_the_loop_revision_downgrades_to_the_revision_before_it(chain: Chain):
    chain.up("4f7a1c2e9b3d")
    chain.down("9318ebdb0db5")
    assert "loop_state" not in chain.tables()
    assert "loop_progress" not in chain.tables()
    assert "scheduler_tasks" in chain.tables()


def test_an_existing_loop_keeps_an_implicit_cap_when_the_cap_column_arrives(chain: Chain):
    chain.up("4f7a1c2e9b3d")
    chain.run(
        "INSERT INTO loop_state (id, run_id, prompt, iteration_index, iterations_remaining,"
        " created_at, updated_at) VALUES (1, 'r', 'p', 0, 3, :at, :at)",
        at=NOW,
    )
    chain.up("8c2e7b1a4f9d")
    assert [bool(v) for (v,) in chain.rows("SELECT cap_explicit FROM loop_state")] == [False]
    assert chain.columns("loop_state")["cap_explicit"]["nullable"] is False


def test_the_cap_column_downgrades_away_and_keeps_the_loop(chain: Chain):
    chain.up("8c2e7b1a4f9d")
    chain.run(
        "INSERT INTO loop_state (id, run_id, prompt, iteration_index, iterations_remaining,"
        " created_at, updated_at) VALUES (1, 'r', 'p', 0, 3, :at, :at)",
        at=NOW,
    )
    chain.down("4f7a1c2e9b3d")
    assert "cap_explicit" not in chain.columns("loop_state")
    assert chain.rows("SELECT run_id FROM loop_state") == [("r",)]


def test_the_background_process_table_keys_on_pid_and_lets_the_group_be_missing(chain: Chain):
    chain.up("b3a8c2e5d4f1")
    assert chain.columns("background_processes")["pgid"]["nullable"] is True
    long_cmd = "echo é" + "x" * 5000
    chain.run(
        "INSERT INTO background_processes (pid, cmd, started_at) VALUES (7, :cmd, 1760000000)",
        cmd=long_cmd,
    )
    assert chain.rows("SELECT cmd, pgid FROM background_processes") == [(long_cmd, None)]
    with pytest.raises(IntegrityError):
        chain.run("INSERT INTO background_processes (pid, cmd, started_at) VALUES (7, 'again', 1)")
    with pytest.raises(IntegrityError):
        chain.run("INSERT INTO background_processes (pid, started_at) VALUES (8, 1)")


def test_the_background_process_table_downgrades_to_the_loop_revision(chain: Chain):
    chain.up("b3a8c2e5d4f1")
    chain.down("8c2e7b1a4f9d")
    assert "background_processes" not in chain.tables()
    assert "loop_state" in chain.tables()


def test_dropping_the_code_index_removes_those_tables_with_their_rows_and_nothing_else(
    chain: Chain,
):
    chain.up("b3a8c2e5d4f1")
    chain.run(
        "INSERT INTO code_index_file_reference (from_uuid, to_uuid, relation, meta)"
        " VALUES ('a', 'b', 'r', '{}')"
    )
    chain.run("INSERT INTO background_processes (pid, cmd, started_at) VALUES (7, 'c', 1)")
    chain.up("d4e5f6a7b8c9")
    assert chain.tables() == {
        "scheduler_tasks",
        "loop_state",
        "loop_progress",
        "background_processes",
    }
    assert chain.rows("SELECT pid FROM background_processes") == [(7,)]


def test_rolling_back_the_code_index_drop_restores_both_tables_empty_with_their_indexes(
    chain: Chain,
):
    chain.up("d4e5f6a7b8c9")
    chain.down("b3a8c2e5d4f1")
    assert {"code_index_commit_metadata", "code_index_file_reference"} <= chain.tables()
    assert chain.indexes("code_index_commit_metadata") == {"idx_cicm_commit"}
    assert chain.indexes("code_index_file_reference") == {"idx_cifr_to", "idx_cifr_relation"}
    assert chain.rows("SELECT * FROM code_index_commit_metadata") == []
    assert chain.rows("SELECT * FROM code_index_file_reference") == []
    chain.run(
        "INSERT INTO code_index_file_reference (from_uuid, to_uuid, relation, meta)"
        " VALUES ('a', 'b', 'r', '{}')"
    )
    with pytest.raises(IntegrityError):
        chain.run(
            "INSERT INTO code_index_file_reference (from_uuid, to_uuid, relation, meta)"
            " VALUES ('a', 'b', 'r', '{}')"
        )


def test_the_process_history_columns_arrive_empty_on_rows_already_there(chain: Chain):
    chain.up("d4e5f6a7b8c9")
    chain.run(
        "INSERT INTO background_processes (pid, cmd, pgid, started_at) VALUES (7, 'c', 70, 5)"
    )
    chain.up("c7f1a9e2d8b4")
    assert chain.rows(
        "SELECT pid, cmd, pgid, started_at, exit_code, finished_at FROM background_processes"
    ) == [(7, "c", 70, 5, None, None)]
    columns = chain.columns("background_processes")
    assert columns["exit_code"]["nullable"] and columns["finished_at"]["nullable"]
    chain.run("UPDATE background_processes SET exit_code = -9, finished_at = 6")
    assert chain.rows("SELECT exit_code, finished_at FROM background_processes") == [(-9, 6)]
    with pytest.raises(IntegrityError):
        chain.run("INSERT INTO background_processes (pid, cmd, started_at) VALUES (7, 'x', 1)")


def test_the_process_history_columns_downgrade_away_and_keep_the_rows(chain: Chain):
    chain.up("c7f1a9e2d8b4")
    chain.run("INSERT INTO background_processes (pid, cmd, started_at) VALUES (7, 'c', 5)")
    chain.down("d4e5f6a7b8c9")
    columns = chain.columns("background_processes")
    assert "exit_code" not in columns and "finished_at" not in columns
    assert chain.rows("SELECT pid, cmd FROM background_processes") == [(7, "c")]
    with pytest.raises(IntegrityError):
        chain.run("INSERT INTO background_processes (pid, cmd, started_at) VALUES (7, 'x', 1)")


def _queue_a_message(chain: Chain) -> None:
    """A row in ``queued_messages`` with every column that must be given."""
    values = {}
    for name, column in chain.columns("queued_messages").items():
        if column["nullable"] or column["default"] is not None or name == "status":
            continue
        values[name] = 1 if isinstance(column["type"], (sa.Integer, sa.BigInteger)) else "x"
    values["id"] = "m1"
    chain.run(
        f"INSERT INTO queued_messages ({', '.join(values)})"  # noqa: S608
        f" VALUES ({', '.join(':' + k for k in values)})",
        **values,
    )


def test_a_queued_message_already_waiting_stays_waiting_when_the_status_column_arrives(
    chain: Chain,
):
    chain.up("f7c2a8e4b1d3")
    assert "status" not in chain.columns("queued_messages")
    _queue_a_message(chain)
    chain.up("a1d6e3f9c2b7")
    assert chain.rows("SELECT id, status FROM queued_messages") == [("m1", "waiting")]
    assert chain.columns("queued_messages")["status"]["nullable"] is False


def test_the_status_column_downgrades_away_and_keeps_the_queued_message(chain: Chain):
    chain.up("a1d6e3f9c2b7")
    _queue_a_message(chain)
    chain.down("f7c2a8e4b1d3")
    assert "status" not in chain.columns("queued_messages")
    assert chain.rows("SELECT id FROM queued_messages") == [("m1",)]


SCOPED = [
    (
        "delivered_events",
        "a9c4e2f7d1b6",
        "d6e1f3a8b2c4",
        "event_id, extension_id, delivered_at",
        "'e1', 'x', 1760000000000",
        "event_id, extension_id, delivered_at",
    ),
    (
        "service_threads",
        "b5d8e1c3a7f9",
        "a9c4e2f7d1b6",
        "extension_id, thread, session_id, title, created_at",
        "'x', 't', 's', 'T', 1",
        "extension_id, thread, session_id, title, created_at",
    ),
    (
        "held_service_posts",
        "b5d8e1c3a7f9",
        "a9c4e2f7d1b6",
        "event_id, session_id, extension_id, client_id, text, held_at",
        "'e1', 's', 'x', 'c', 'hello é', 1",
        "event_id, session_id, extension_id, client_id, text, held_at",
    ),
    (
        "waiting_spawns",
        "b8e2f4a6d1c3",
        "a1d6e3f9c2b7",
        "run_id, session_id, agent_session_id, agent_name, parents, created_at",
        "'r1', 's', 'a', 'n', '[]', 1",
        "run_id, session_id, agent_session_id, agent_name, parents, created_at",
    ),
    (
        "turn_results",
        "c4d7a2e9f5b1",
        "b8e2f4a6d1c3",
        "run_id, tool_call_id, result, created_at",
        "'r1', 'call', 'out', 1",
        "run_id, tool_call_id, result, created_at",
    ),
    (
        "extension_settings",
        "d6e1f3a8b2c4",
        "c4d7a2e9f5b1",
        "extension_id, values, updated_at",
        "'x', '{}', 1",
        "extension_id, values, updated_at",
    ),
]
IDS = [row[0] for row in SCOPED]


def _insert(chain: Chain, table: str, columns: str, values: str, scope=("", "", "")) -> None:
    quoted = ", ".join(f'"{c.strip()}"' for c in columns.split(","))
    chain.run(
        f"INSERT INTO {table} (org, producer, project, {quoted}) VALUES (:o, :p, :j, {values})",  # noqa: S608
        o=scope[0],
        p=scope[1],
        j=scope[2],
    )


@pytest.mark.parametrize(("table", "revision", "before", "columns", "values", "_"), SCOPED, ids=IDS)
def test_a_scoped_table_refuses_a_repeat_in_one_scope_and_allows_it_in_another(
    chain: Chain, table, revision, before, columns, values, _
):
    chain.up(revision)
    _insert(chain, table, columns, values, ("o", "p", "j"))
    for other in (("o2", "p", "j"), ("o", "p2", "j"), ("o", "p", "j2"), ("", "", "")):
        _insert(chain, table, columns, values, other)
    with pytest.raises(IntegrityError):
        _insert(chain, table, columns, values, ("o", "p", "j"))


@pytest.mark.parametrize(("table", "revision", "before", "columns", "values", "_"), SCOPED, ids=IDS)
def test_a_scoped_table_leaves_the_scope_blank_when_none_is_given(
    chain: Chain, table, revision, before, columns, values, _
):
    chain.up(revision)
    quoted = ", ".join(f'"{c.strip()}"' for c in columns.split(","))
    chain.run(f"INSERT INTO {table} ({quoted}) VALUES ({values})")  # noqa: S608
    assert chain.rows(f"SELECT org, producer, project FROM {table}") == [("", "", "")]  # noqa: S608
    with pytest.raises(IntegrityError):
        chain.run(f"INSERT INTO {table} ({quoted}) VALUES ({values})")  # noqa: S608


@pytest.mark.parametrize(("table", "revision", "before", "columns", "values", "_"), SCOPED, ids=IDS)
def test_a_scoped_table_is_taken_back_by_its_downgrade(
    chain: Chain, table, revision, before, columns, values, _
):
    chain.up(revision)
    assert table in chain.tables()
    chain.down(before)
    assert table not in chain.tables()


def test_the_service_thread_revision_creates_both_its_tables_and_downgrade_drops_both(
    chain: Chain,
):
    chain.up("a9c4e2f7d1b6")
    assert {"service_threads", "held_service_posts"}.isdisjoint(chain.tables())
    chain.up("b5d8e1c3a7f9")
    assert {"service_threads", "held_service_posts"} <= chain.tables()
    chain.down("a9c4e2f7d1b6")
    assert {"service_threads", "held_service_posts"}.isdisjoint(chain.tables())
    assert "delivered_events" in chain.tables()


def test_delivered_event_times_fit_milliseconds_and_waiting_spawns_keep_a_long_chain(
    chain: Chain,
):
    chain.up("c4d7a2e9f5b1")
    parents = "p" * 100_000
    chain.run(
        "INSERT INTO waiting_spawns (run_id, session_id, agent_session_id, agent_name,"
        " parents, created_at) VALUES ('r', 's', 'a', 'n', :parents, 1)",
        parents=parents,
    )
    chain.run(
        "INSERT INTO turn_results (run_id, tool_call_id, result, created_at)"
        " VALUES ('r', 'c', :r, 1)",
        r="é" * 100_000,
    )
    assert chain.rows("SELECT length(parents) FROM waiting_spawns") == [(100_000,)]
    assert chain.rows("SELECT length(result) FROM turn_results") == [(100_000,)]
    chain.up("a9c4e2f7d1b6")
    chain.run(
        "INSERT INTO delivered_events (event_id, extension_id, delivered_at)"
        " VALUES ('e', 'x', 1760000000000)"
    )
    assert chain.rows("SELECT delivered_at FROM delivered_events") == [(1760000000000,)]


def test_one_tool_call_id_is_kept_once_per_run(chain: Chain):
    chain.up("c4d7a2e9f5b1")
    insert = "INSERT INTO turn_results (run_id, tool_call_id, result, created_at) VALUES (:r, :c, 'x', 1)"
    chain.run(insert, r="run-1", c="call")
    chain.run(insert, r="run-2", c="call")
    chain.run(insert, r="run-1", c="other")
    with pytest.raises(IntegrityError):
        chain.run(insert, r="run-1", c="call")


def test_one_thread_key_is_kept_once_per_extension(chain: Chain):
    chain.up("b5d8e1c3a7f9")
    insert = (
        "INSERT INTO service_threads (extension_id, thread, session_id, title, created_at)"
        " VALUES (:x, :t, 's', 'T', 1)"
    )
    chain.run(insert, x="ext-1", t="thread")
    chain.run(insert, x="ext-2", t="thread")
    chain.run(insert, x="ext-1", t="other")
    with pytest.raises(IntegrityError):
        chain.run(insert, x="ext-1", t="thread")


def test_two_paused_runs_in_one_scope_are_both_kept_by_the_waiting_spawn_table(chain: Chain):
    chain.up("b8e2f4a6d1c3")
    columns = "run_id, session_id, agent_session_id, agent_name, parents, created_at"
    _insert(chain, "waiting_spawns", columns, "'r1', 's', 'a', 'n', '[]', 1", ("o", "p", "j"))
    _insert(chain, "waiting_spawns", columns, "'r2', 's', 'a', 'n', '[]', 1", ("o", "p", "j"))
    assert chain.rows("SELECT run_id FROM waiting_spawns ORDER BY run_id") == [("r1",), ("r2",)]
