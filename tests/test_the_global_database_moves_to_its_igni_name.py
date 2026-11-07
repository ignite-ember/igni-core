"""The global database is ``igni.db``; one called ``ember.db`` is moved there.

It was named before the product settled on igni. A person upgrading keeps
which folder each session is in, their window state and their local
notifications: the old file, with SQLite's journal files beside it, is moved
the first time igni asks where the global database is.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from igni_core.core.data_paths import GLOBAL_DB, LEGACY_GLOBAL_DB, global_db_path


def _database_with_a_row(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute("create table kept (value text)")
        db.execute("insert into kept values ('the person''s state')")


def test_a_fresh_data_root_gets_igni_db(tmp_path: Path) -> None:
    assert global_db_path(tmp_path) == tmp_path / GLOBAL_DB == tmp_path / "igni.db"


def test_ember_db_is_moved_with_its_journal_files_and_keeps_its_rows(tmp_path: Path) -> None:
    _database_with_a_row(tmp_path / LEGACY_GLOBAL_DB)
    (tmp_path / f"{LEGACY_GLOBAL_DB}-wal").write_bytes(b"")
    path = global_db_path(tmp_path)
    assert path == tmp_path / "igni.db"
    assert not (tmp_path / "ember.db").exists()
    assert (tmp_path / "igni.db-wal").exists() and not (tmp_path / "ember.db-wal").exists()
    with sqlite3.connect(path) as db:
        assert db.execute("select value from kept").fetchall() == [("the person's state",)]


def test_an_existing_igni_db_is_never_overwritten(tmp_path: Path) -> None:
    _database_with_a_row(tmp_path / "igni.db")
    (tmp_path / "ember.db").write_bytes(b"older")
    assert global_db_path(tmp_path) == tmp_path / "igni.db"
    assert (tmp_path / "ember.db").read_bytes() == b"older"


def test_a_move_that_fails_keeps_using_the_old_file(tmp_path: Path, monkeypatch) -> None:
    _database_with_a_row(tmp_path / "ember.db")

    def refuse(src: object, dst: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(os, "replace", refuse)
    assert global_db_path(tmp_path) == tmp_path / "ember.db"
