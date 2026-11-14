"""How each dialect's :class:`SharedStorage` is found, cached and replaced.

SQLite's is the core's own. Another dialect's is the entry point of its name
in the ``igni.storage`` group, which a package such as igni-cloud declares;
the entry points are stood in for here, since which packages are installed
is what these tests must control.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agno.db.sqlite import AsyncSqliteDb

from igni_core.core.db.providers import (
    SharedStorage,
    SharedStorages,
    SqliteStorage,
    StorageUnavailable,
)
from igni_core.core.db.scope import StorageScope
from igni_core.core.db.urls import DatabaseUrl, Dialect
from tests.test_agno_tables_are_migrated_once_and_quietly import (
    SESSIONS,
    _version,
    _write_agno_2_6_file,
)


class FakeCloudStorage(SharedStorage):
    instances = 0

    def __init__(self) -> None:
        type(self).instances += 1

    def agno_db(self, **_: Any) -> Any:
        return "agno-db-on-the-server"


class FirstCloudStorage(FakeCloudStorage):
    pass


def entry(name: str, produces: Any, value: str = "igni_cloud.storage:PostgresStorage"):
    loads: list[str] = []

    def load() -> Any:
        loads.append(name)
        if isinstance(produces, Exception):
            raise produces
        return produces

    return SimpleNamespace(name=name, value=value, load=load, loads=loads)


@pytest.fixture(autouse=True)
def _nothing_found_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SharedStorages, "_found", {})
    FakeCloudStorage.instances = 0


def declare(monkeypatch: pytest.MonkeyPatch, *entries: Any) -> None:
    def entry_points(*, group: str) -> list[Any]:
        assert group == "igni.storage"
        return list(entries)

    monkeypatch.setattr("igni_core.core.db.providers.entry_points", entry_points)


def test_sqlite_uses_the_cores_own_storage_without_asking_any_package(
    monkeypatch: pytest.MonkeyPatch,
):
    declare(monkeypatch)
    storage = SharedStorages.for_dialect(Dialect.SQLITE)
    assert type(storage) is SqliteStorage
    assert SharedStorages.for_url(DatabaseUrl.for_file("/tmp/x.db")) is storage


def test_a_declared_entry_point_is_loaded_instantiated_and_kept(monkeypatch: pytest.MonkeyPatch):
    declared = entry("postgresql", FakeCloudStorage)
    declare(monkeypatch, entry("mysql", FakeCloudStorage), declared)
    url = DatabaseUrl.parse("postgresql://igni:pw@db.internal/igni")
    first = SharedStorages.for_url(url)
    assert isinstance(first, FakeCloudStorage)
    assert SharedStorages.for_dialect(Dialect.POSTGRESQL) is first
    assert declared.loads == ["postgresql"]
    assert FakeCloudStorage.instances == 1


def test_when_several_entry_points_share_a_name_the_first_wins(monkeypatch: pytest.MonkeyPatch):
    declare(
        monkeypatch, entry("postgresql", FirstCloudStorage), entry("postgresql", FakeCloudStorage)
    )
    assert type(SharedStorages.for_dialect(Dialect.POSTGRESQL)) is FirstCloudStorage


def test_an_entry_point_that_makes_something_else_is_refused_by_name_and_not_kept(
    monkeypatch: pytest.MonkeyPatch,
):
    declare(monkeypatch, entry("postgresql", dict, value="somewhere.else:Thing"))
    with pytest.raises(StorageUnavailable, match="somewhere.else:Thing is not a SharedStorage"):
        SharedStorages.for_dialect(Dialect.POSTGRESQL)
    assert Dialect.POSTGRESQL not in SharedStorages._found
    declare(monkeypatch, entry("postgresql", FakeCloudStorage))
    assert isinstance(SharedStorages.for_dialect(Dialect.POSTGRESQL), FakeCloudStorage)


def test_an_entry_point_that_fails_to_load_fails_the_lookup_and_is_tried_again(
    monkeypatch: pytest.MonkeyPatch,
):
    declare(monkeypatch, entry("postgresql", ImportError("igni_cloud is half installed")))
    with pytest.raises(ImportError, match="half installed"):
        SharedStorages.for_dialect(Dialect.POSTGRESQL)
    assert Dialect.POSTGRESQL not in SharedStorages._found
    declare(monkeypatch, entry("postgresql", FakeCloudStorage))
    assert isinstance(SharedStorages.for_dialect(Dialect.POSTGRESQL), FakeCloudStorage)


def test_without_a_package_for_the_dialect_the_user_is_told_what_to_install(
    monkeypatch: pytest.MonkeyPatch,
):
    declare(monkeypatch, entry("mysql", FakeCloudStorage))
    with pytest.raises(StorageUnavailable, match="install igni-cloud for PostgreSQL") as refusal:
        SharedStorages.for_dialect(Dialect.POSTGRESQL)
    assert "postgresql" in str(refusal.value)
    assert isinstance(refusal.value, RuntimeError)


def test_install_replaces_what_was_found(monkeypatch: pytest.MonkeyPatch):
    declare(monkeypatch, entry("postgresql", FakeCloudStorage))
    found = SharedStorages.for_dialect(Dialect.POSTGRESQL)
    replacement = FirstCloudStorage()
    SharedStorages.install(Dialect.POSTGRESQL, replacement)
    assert SharedStorages.for_dialect(Dialect.POSTGRESQL) is replacement
    assert replacement is not found


def test_install_before_any_lookup_means_no_entry_point_is_asked(monkeypatch: pytest.MonkeyPatch):
    declared = entry("postgresql", FakeCloudStorage)
    declare(monkeypatch, declared)
    replacement = FirstCloudStorage()
    SharedStorages.install(Dialect.POSTGRESQL, replacement)
    assert SharedStorages.for_url(DatabaseUrl.parse("postgresql://u:p@h/db")) is replacement
    assert declared.loads == []


def test_a_storage_keeps_attachments_as_files_and_prepares_nothing_by_default():
    storage = FakeCloudStorage()
    assert storage.attachments(SimpleNamespace(), Path("/work")) is None  # type: ignore[arg-type]
    assert storage.prepare(DatabaseUrl.parse("postgresql://u:p@h/db")) is None


def test_the_sqlite_storage_makes_the_directory_and_returns_agnos_database(tmp_path: Path):
    path = tmp_path / "not" / "yet" / "state.db"
    db = SqliteStorage().agno_db(
        url=DatabaseUrl.for_file(path),
        scope=StorageScope.local(),
        session_table="s_tab",
        memory_table="m_tab",
    )
    assert isinstance(db, AsyncSqliteDb)
    assert path.parent.is_dir()
    assert not path.exists()
    assert db.db_file == str(path.resolve())
    assert (db.session_table_name, db.memory_table_name) == ("s_tab", "m_tab")


def test_the_sqlite_storage_migrates_an_existing_agno_2_6_file_before_handing_it_over(
    tmp_path: Path,
):
    path = _write_agno_2_6_file(tmp_path / "state.db")
    assert _version(path) == "2.5.6"
    SqliteStorage().agno_db(
        url=DatabaseUrl.for_file(path),
        scope=StorageScope.local(),
        session_table=SESSIONS,
        memory_table="igni_memories",
    )
    assert _version(path) == "3.0.0"
