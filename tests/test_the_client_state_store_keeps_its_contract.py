"""``ClientStateStore``: a window's key/value state, scoped, upserted, never faked.

Real databases on both dialects. The failure wording (driver text never
reaches the result) is in ``test_a_failed_state_write_is_told.py``; the
two-scope write isolation is in ``test_storage_backends.py``. These pin
the rest of the contract: refusals, upsert, delete, scoping of delete
and read, the empty project of a window, the table's shape.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from igni_core.core.db.database import Database
from igni_core.core.db.locator import StorageLocator
from igni_core.core.db.urls import DatabaseUrl
from igni_core.core.session.client_state import (
    ClientStateModel,
    ClientStateRead,
    ClientStateResult,
    ClientStateStore,
)

from .storage_backends import Scopes


def _rows(db: Database) -> list[ClientStateModel]:
    with db.sync_session() as session:
        return list(
            session.scalars(select(ClientStateModel).where(db.scope.where(ClientStateModel)))
        )


def test_a_client_that_stored_nothing_reads_as_ok_and_empty(database_url: DatabaseUrl) -> None:
    mine, _ = Scopes.pair(database_url)

    read = ClientStateStore(mine).get_for_client("window")

    assert read.ok is True
    assert read.values == {}
    assert read.error == ""


def test_a_read_returns_every_key_of_that_client_only(database_url: DatabaseUrl) -> None:
    mine, _ = Scopes.pair(database_url)
    store = ClientStateStore(mine)
    store.set_value("w1", "sidebar", "open")
    store.set_value("w1", "draft", "hello")
    store.set_value("w2", "sidebar", "closed")

    assert store.get_for_client("w1").values == {"sidebar": "open", "draft": "hello"}
    assert store.get_for_client("w2").values == {"sidebar": "closed"}


def test_writing_a_key_again_replaces_it_and_refreshes_when(
    database_url: DatabaseUrl, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine, _ = Scopes.pair(database_url)
    store = ClientStateStore(mine)
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    store.set_value("w", "k", "one")
    monkeypatch.setattr(time, "time", lambda: 2000.0)
    store.set_value("w", "k", "two")

    rows = _rows(mine)

    assert [(r.key, r.value, r.updated_at) for r in rows] == [("k", "two", 2000)]


def test_unicode_and_very_large_values_come_back_unchanged(database_url: DatabaseUrl) -> None:
    mine, _ = Scopes.pair(database_url)
    store = ClientStateStore(mine)
    big = "x" * 200_000
    store.set_value("w", "unicode", "naïve — 日本語 🙂")
    store.set_value("w", "big", big)

    values = store.get_for_client("w").values

    assert values == {"unicode": "naïve — 日本語 🙂", "big": big}


def test_a_value_may_be_empty_text(database_url: DatabaseUrl) -> None:
    mine, _ = Scopes.pair(database_url)
    store = ClientStateStore(mine)

    assert store.set_value("w", "k", "").ok
    assert store.get_for_client("w").values == {"k": ""}


def test_delete_removes_one_key_and_an_absent_key_is_not_a_failure(
    database_url: DatabaseUrl,
) -> None:
    mine, _ = Scopes.pair(database_url)
    store = ClientStateStore(mine)
    store.set_value("w", "a", "1")
    store.set_value("w", "b", "2")

    assert store.delete_value("w", "a") == ClientStateResult(ok=True)
    assert store.delete_value("w", "never-there") == ClientStateResult(ok=True)

    assert store.get_for_client("w").values == {"b": "2"}


def test_a_delete_never_touches_another_clients_key_or_another_scope(
    database_url: DatabaseUrl,
) -> None:
    mine, theirs = Scopes.pair(database_url)
    ClientStateStore(mine).set_value("w1", "k", "mine")
    ClientStateStore(mine).set_value("w2", "k", "other-window")
    ClientStateStore(theirs).set_value("w1", "k", "theirs")

    ClientStateStore(mine).delete_value("w1", "k")

    assert ClientStateStore(mine).get_for_client("w1").values == {}
    assert ClientStateStore(mine).get_for_client("w2").values == {"k": "other-window"}
    assert ClientStateStore(theirs).get_for_client("w1").values == {"k": "theirs"}


def test_a_read_in_one_scope_does_not_see_another_scopes_state(database_url: DatabaseUrl) -> None:
    mine, theirs = Scopes.pair(database_url)
    ClientStateStore(theirs).set_value("w", "k", "theirs")

    assert ClientStateStore(mine).get_for_client("w").values == {}


@pytest.mark.parametrize(("client", "key"), [("", "k"), ("w", ""), ("", "")])
def test_a_write_or_delete_without_a_client_or_a_key_is_refused_and_stores_nothing(
    database_url: DatabaseUrl, client: str, key: str
) -> None:
    mine, _ = Scopes.pair(database_url)
    store = ClientStateStore(mine)

    written = store.set_value(client, key, "v")
    deleted = store.delete_value(client, key)

    assert (written.ok, written.error) == (False, "a client id and a key are required")
    assert (deleted.ok, deleted.error) == (False, "a client id and a key are required")
    assert _rows(mine) == []


def test_a_read_without_a_client_is_refused(database_url: DatabaseUrl) -> None:
    mine, _ = Scopes.pair(database_url)

    read = ClientStateStore(mine).get_for_client("")

    assert (read.ok, read.error, read.values) == (False, "a client id is required", {})


def test_the_store_opens_from_a_path_a_string_or_a_database(tmp_path: Path) -> None:
    for source in (tmp_path / "a.db", str(tmp_path / "b.db"), Database.of(tmp_path / "c.db")):
        store = ClientStateStore(source)
        assert store.set_value("w", "k", "v").ok
        assert store.get_for_client("w").values == {"k": "v"}


def test_a_window_is_kept_in_the_global_database_with_no_project(tmp_path: Path) -> None:
    locator = StorageLocator(database_url=None, data_dir=tmp_path)
    store = ClientStateStore.from_locator(locator)

    assert store.set_value("w", "k", "v").ok

    # The same file the locator names as the global one, read back with a
    # handle of its own: the row is there, under no project.
    again = locator.global_database()
    assert [(r.client_id, r.key, r.value, r.project) for r in _rows(again)] == [("w", "k", "v", "")]
    assert ClientStateStore(again).get_for_client("w").values == {"k": "v"}


def test_the_table_is_keyed_by_scope_client_and_key() -> None:
    table = ClientStateModel.__table__
    key = [column.name for column in table.primary_key.columns]

    assert key == ["org", "producer", "project", "client_id", "key"]
    assert table.name == "ember_client_state"


def test_the_results_are_frozen() -> None:
    result = ClientStateResult()
    read = ClientStateRead(values={"k": "v"})

    with pytest.raises(ValidationError):
        result.ok = False  # type: ignore[misc]
    with pytest.raises(ValidationError):
        read.error = "x"  # type: ignore[misc]
