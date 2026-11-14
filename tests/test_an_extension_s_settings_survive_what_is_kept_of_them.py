"""An extension's stored settings values, on both backends.

An administrator's values sit beside the bundle (desktop) or in one row
per extension (cloud). Whatever is found there, the extension still
starts: a file or a row that is missing, unreadable, not JSON or not an
object reads as no values, so its declared defaults apply. What is
written reads back, replaces what was there, and is another
producer's business never.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from sqlalchemy import select

from igni_core.core.db.database import Database
from igni_core.core.db.urls import DatabaseUrl
from igni_core.extensions.settings_values import (
    DatabaseSettingsValues,
    ExtensionSettingsModel,
    FileSettingsValues,
)
from tests.storage_backends import Scopes

# ── On the desktop: a file beside the bundles ──────────────────────


def test_no_file_reads_as_no_values(tmp_path: Path):
    assert FileSettingsValues(tmp_path).read("acme.notes") == {}


def test_the_file_is_sorted_indented_json_with_a_trailing_newline(tmp_path: Path):
    FileSettingsValues(tmp_path).write("acme.notes", {"b": 1, "a": {"z": True, "y": "x"}})
    assert (tmp_path / "acme.notes.settings.json").read_text(encoding="utf-8") == (
        '{\n  "a": {\n    "y": "x",\n    "z": true\n  },\n  "b": 1\n}\n'
    )


def test_writing_makes_the_store_directory_when_it_is_not_there_yet(tmp_path: Path):
    root = tmp_path / "not" / "yet"
    FileSettingsValues(root).write("acme.notes", {"k": "v"})
    assert FileSettingsValues(root).read("acme.notes") == {"k": "v"}


def test_values_read_back_as_they_were_written_and_a_second_write_replaces_them(tmp_path: Path):
    values = FileSettingsValues(tmp_path)
    values.write("acme.notes", {"name": "Zoë", "nested": {"list": [1, 2]}, "old": 1})
    assert values.read("acme.notes") == {"name": "Zoë", "nested": {"list": [1, 2]}, "old": 1}
    values.write("acme.notes", {"new": 2})
    assert values.read("acme.notes") == {"new": 2}


def test_each_extension_has_its_own_file(tmp_path: Path):
    values = FileSettingsValues(tmp_path)
    values.write("acme.a", {"k": 1})
    values.write("acme.b", {"k": 2})
    assert (values.read("acme.a"), values.read("acme.b")) == ({"k": 1}, {"k": 2})
    values.delete("acme.a")
    assert (values.read("acme.a"), values.read("acme.b")) == ({}, {"k": 2})


def test_deleting_what_is_not_there_is_harmless(tmp_path: Path):
    FileSettingsValues(tmp_path).delete("acme.notes")
    FileSettingsValues(tmp_path / "no-such-dir").delete("acme.notes")


def test_a_file_that_is_not_json_reads_as_no_values_and_says_so(tmp_path: Path, caplog):
    (tmp_path / "acme.notes.settings.json").write_text("{ not json")
    with caplog.at_level(logging.WARNING):
        assert FileSettingsValues(tmp_path).read("acme.notes") == {}
    assert "ignoring" in caplog.text and "acme.notes.settings.json" in caplog.text


@pytest.mark.parametrize("content", ["[1, 2]", '"text"', "3", "null", ""])
def test_a_file_that_is_not_an_object_reads_as_no_values(tmp_path: Path, content: str):
    (tmp_path / "acme.notes.settings.json").write_text(content)
    assert FileSettingsValues(tmp_path).read("acme.notes") == {}


def test_a_file_that_is_not_text_reads_as_no_values(tmp_path: Path, caplog):
    (tmp_path / "acme.notes.settings.json").write_bytes(b'{"k": "\xff\xfe"}')
    with caplog.at_level(logging.WARNING):
        assert FileSettingsValues(tmp_path).read("acme.notes") == {}
    assert "ignoring" in caplog.text


def test_a_file_that_cannot_be_read_reads_as_no_values(tmp_path: Path):
    path = tmp_path / "acme.notes.settings.json"
    path.write_text('{"k": 1}')
    path.chmod(0)
    try:
        if _can_read(path):
            pytest.skip("this account reads files whatever their mode")
        assert FileSettingsValues(tmp_path).read("acme.notes") == {}
    finally:
        path.chmod(0o600)


def _can_read(path: Path) -> bool:
    try:
        path.read_text()
    except OSError:
        return False
    return True


def test_a_directory_where_the_file_should_be_reads_as_no_values(tmp_path: Path):
    (tmp_path / "acme.notes.settings.json").mkdir()
    assert FileSettingsValues(tmp_path).read("acme.notes") == {}


# ── In the cloud: a row per extension, in the producer's scope ─────


def rows_of(db: Database) -> list[ExtensionSettingsModel]:
    with db.sync_session() as session:
        return list(
            session.scalars(
                select(ExtensionSettingsModel).where(db.scope.where(ExtensionSettingsModel))
            )
        )


def plant(db: Database, extension_id: str, raw: str) -> None:
    with db.sync_session() as session, session.begin():
        session.add(
            ExtensionSettingsModel(
                **db.scope.stamp(), extension_id=extension_id, values=raw, updated_at=0
            )
        )


def test_a_row_reads_back_and_a_second_write_replaces_it(database_url: DatabaseUrl):
    mine, _ = Scopes.pair(database_url)
    values = DatabaseSettingsValues(mine)
    assert values.read("acme.notes") == {}
    values.write("acme.notes", {"name": "Zoë", "nested": {"a": [1]}, "old": True})
    assert values.read("acme.notes") == {"name": "Zoë", "nested": {"a": [1]}, "old": True}
    values.write("acme.notes", {"new": 1})
    assert values.read("acme.notes") == {"new": 1}
    rows = rows_of(mine)
    assert len(rows) == 1, "a second write updates the row, it does not add one"


def test_a_write_stamps_when_it_was_made(database_url: DatabaseUrl):
    mine, _ = Scopes.pair(database_url)
    DatabaseSettingsValues(mine).write("acme.notes", {"k": 1})
    (row,) = rows_of(mine)
    assert row.updated_at > 1_700_000_000


def test_another_producer_neither_reads_nor_deletes_a_row(database_url: DatabaseUrl):
    mine, theirs = Scopes.pair(database_url)
    DatabaseSettingsValues(mine).write("acme.notes", {"k": "mine"})
    DatabaseSettingsValues(theirs).write("acme.notes", {"k": "theirs"})

    DatabaseSettingsValues(theirs).delete("acme.notes")

    assert DatabaseSettingsValues(theirs).read("acme.notes") == {}
    assert DatabaseSettingsValues(mine).read("acme.notes") == {"k": "mine"}


def test_deleting_what_is_not_there_is_harmless_in_the_cloud(database_url: DatabaseUrl):
    mine, _ = Scopes.pair(database_url)
    DatabaseSettingsValues(mine).delete("acme.notes")


def test_each_extension_has_its_own_row(database_url: DatabaseUrl):
    mine, _ = Scopes.pair(database_url)
    values = DatabaseSettingsValues(mine)
    values.write("acme.a", {"k": 1})
    values.write("acme.b", {"k": 2})
    values.delete("acme.a")
    assert (values.read("acme.a"), values.read("acme.b")) == ({}, {"k": 2})


@pytest.mark.parametrize("raw", ["{ not json", "[1]", '"text"', "5", "null", ""])
def test_a_row_that_is_not_an_object_reads_as_no_values(database_url: DatabaseUrl, raw: str):
    mine, _ = Scopes.pair(database_url)
    plant(mine, "acme.notes", raw)
    assert DatabaseSettingsValues(mine).read("acme.notes") == {}


def test_what_is_written_is_a_json_object(database_url: DatabaseUrl):
    mine, _ = Scopes.pair(database_url)
    DatabaseSettingsValues(mine).write("acme.notes", {"b": 1, "a": 2})
    (row,) = rows_of(mine)
    raw = row.values
    assert json.loads(raw) == {"a": 2, "b": 1}
    assert raw.index('"a"') < raw.index('"b"')
