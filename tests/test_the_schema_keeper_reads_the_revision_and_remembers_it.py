"""What :class:`SchemaRevision` reads and :class:`SchemaKeeper` does about it.

A database whose revision is not the head is handed to the migrator, a
current one is remembered so that a second look costs nothing, and a
migration that fails is not remembered. The migrator itself is a recorder
here: the real alembic run is pinned by ``test_migrations_stay_off_the_start_path.py``.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from igni_core.core.db import schema
from igni_core.core.db.schema import HEAD_REVISION, SchemaKeeper, SchemaRevision
from igni_core.core.db.urls import DatabaseUrl

#: A server nobody is listening on: connecting is refused at once.
UNREACHABLE = "postgresql://igni:pw@127.0.0.1:1/igni"


def write_revision(path: Path, revision: str | None) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        if revision is not None:
            connection.execute("INSERT INTO alembic_version VALUES (?)", (revision,))


@pytest.fixture
def migrator(monkeypatch: pytest.MonkeyPatch):
    """The module the keeper imports when a database is behind, as a recorder."""
    upgraded: list[DatabaseUrl] = []
    failing: list[bool] = [False]

    def upgrade_to_head(url: DatabaseUrl) -> None:
        upgraded.append(url)
        if failing[0]:
            raise RuntimeError("the migration failed")

    monkeypatch.setitem(
        sys.modules, "igni_test_migrations", SimpleNamespace(upgrade_to_head=upgrade_to_head)
    )
    monkeypatch.setattr(schema, "MIGRATIONS_MODULE", "igni_test_migrations")
    return SimpleNamespace(upgraded=upgraded, failing=failing)


def test_the_revision_is_what_alembic_version_says(tmp_path: Path):
    path = tmp_path / "state.db"
    write_revision(path, HEAD_REVISION)
    assert SchemaRevision.of(path) == HEAD_REVISION
    assert SchemaRevision.is_current(path)


def test_an_alembic_version_table_with_no_row_has_no_revision(tmp_path: Path):
    path = tmp_path / "state.db"
    write_revision(path, None)
    assert SchemaRevision.of(path) is None
    assert not SchemaRevision.is_current(path)


def test_asking_about_a_file_that_does_not_exist_does_not_create_it(tmp_path: Path):
    path = tmp_path / "nowhere" / "state.db"
    assert SchemaRevision.of(path) is None
    assert not path.exists()
    assert not path.parent.exists()


def test_a_server_that_cannot_be_reached_has_no_revision():
    assert SchemaRevision.of(UNREACHABLE) is None
    assert not SchemaRevision.is_current(UNREACHABLE)


def test_a_database_behind_the_head_is_not_current(tmp_path: Path):
    path = tmp_path / "state.db"
    write_revision(path, "0000behind")
    assert not SchemaRevision.is_current(path)


def test_a_database_ahead_of_the_head_is_not_current_and_is_handed_to_the_migrator(
    tmp_path: Path, migrator: SimpleNamespace
):
    path = tmp_path / "state.db"
    write_revision(path, "ffffnewer")
    assert SchemaRevision.of(path) == "ffffnewer"
    assert not SchemaRevision.is_current(path)
    SchemaKeeper().ensure_current(path)
    assert migrator.upgraded == [DatabaseUrl.for_file(path)]


def test_a_current_database_never_reaches_the_migrator(tmp_path: Path, migrator: SimpleNamespace):
    path = tmp_path / "state.db"
    write_revision(path, HEAD_REVISION)
    SchemaKeeper().ensure_current(path)
    assert migrator.upgraded == []


def test_an_unreadable_server_is_handed_to_the_migrator(migrator: SimpleNamespace):
    SchemaKeeper().ensure_current(UNREACHABLE)
    assert migrator.upgraded == [DatabaseUrl.parse(UNREACHABLE)]


def test_a_database_seen_current_is_not_read_or_migrated_again(
    tmp_path: Path, migrator: SimpleNamespace
):
    path = tmp_path / "state.db"
    keeper = SchemaKeeper()
    keeper.ensure_current(path)  # new: migrated
    assert len(migrator.upgraded) == 1
    # Were it read again, a file that is gone would be new again.
    keeper.ensure_current(path)
    keeper.ensure_current(tmp_path / "." / "state.db")
    assert len(migrator.upgraded) == 1
    # Another keeper has seen nothing.
    SchemaKeeper().ensure_current(path)
    assert len(migrator.upgraded) == 2


def test_a_failed_migration_is_raised_and_the_next_call_tries_again(
    tmp_path: Path, migrator: SimpleNamespace
):
    path = tmp_path / "state.db"
    keeper = SchemaKeeper()
    migrator.failing[0] = True
    with pytest.raises(RuntimeError, match="migration failed"):
        keeper.ensure_current(path)
    migrator.failing[0] = False
    keeper.ensure_current(path)
    assert len(migrator.upgraded) == 2
    keeper.ensure_current(path)
    assert len(migrator.upgraded) == 2


def test_the_process_keeper_is_the_one_every_database_handle_uses():
    from igni_core.core.db import database

    assert database.keeper is schema.keeper
    assert isinstance(schema.keeper, SchemaKeeper)
