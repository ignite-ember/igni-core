"""The database package's two shared objects: the declarative base and the engines registry.

Every igni table registers on :class:`Base`, so alembic and the programmatic
upgrade see them all; every database the process opens draws its engines
from the one ``engines`` registry, which importing opens nothing on.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import igni_core.core.db.engine as engine_module
from igni_core.core.db import engine_registry
from igni_core.core.db.base import Base
from igni_core.core.db.database import Database
from igni_core.core.db.engine import engines
from igni_core.core.db.engine_registry import EngineRegistry
from igni_core.core.db.schema import SchemaRevision
from igni_core.core.db.urls import DatabaseUrl


def test_base_is_a_declarative_base_whose_metadata_holds_the_igni_tables(tmp_path: Path):
    assert issubclass(Base, DeclarativeBase)
    Database(tmp_path / "state.db")
    created = set(inspect(create_engine(f"sqlite:///{tmp_path / 'state.db'}")).get_table_names())
    assert "attachments" in created
    assert created - {"alembic_version"} <= set(Base.metadata.tables)


def test_a_model_declared_on_base_registers_its_table_there():
    class Probe(Base):
        __tablename__ = "the_db_package_probe"
        id: Mapped[int] = mapped_column(primary_key=True)

    try:
        assert "the_db_package_probe" in Base.metadata.tables
    finally:
        Base.metadata.remove(Probe.__table__)  # type: ignore[arg-type]


def test_the_base_docstring_uses_the_product_name():
    assert "ember" not in (Base.__doc__ or "").lower()


def test_the_engine_module_exports_the_registry_and_nothing_else():
    assert engine_module.__all__ == ["engines"]
    assert type(engines) is EngineRegistry
    public = {name for name in vars(engine_module) if not name.startswith("_")}
    assert public - {"annotations", "EngineRegistry", "engines"} == set()


def test_the_registry_module_has_no_process_wide_engine_helpers_of_its_own():
    assert sorted(engine_registry.__all__) == [
        "DatabaseTarget",
        "EngineRegistry",
        "PostgresConnectionSetup",
        "SqliteConnectionSetup",
    ]
    for old in ("get_engine", "get_sessionmaker", "get_async_engine", "dispose_sync", "engines"):
        assert not hasattr(engine_registry, old)


def test_the_schema_check_and_the_database_handle_use_the_same_engine(tmp_path: Path):
    path = tmp_path / "state.db"
    Database(path)
    assert SchemaRevision.of(path) is not None
    assert engines.get_engine(path) is engines.get_engine(DatabaseUrl.for_file(path))
    assert Database(path)._sync_sessionmaker.kw["bind"] is engines.get_engine(path)


def test_importing_the_package_opens_no_database(tmp_path: Path):
    code = textwrap.dedent(
        """
        from igni_core.core.db.engine import engines
        import igni_core.core.db.database, igni_core.core.db.schema, igni_core.core.db.locator
        assert not (engines._sync_engines or engines._async_engines), "an engine was made"
        print("quiet")
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "quiet"


def test_reading_a_revision_opens_the_pool_the_process_registry_owns(tmp_path: Path):
    """A second registry's pool on the file would fight the first for its lock."""
    path = tmp_path / "state.db"
    sqlite3.connect(path).close()
    assert SchemaRevision.of(path) is None
    assert DatabaseUrl.for_file(path).url in engines._sync_engines
