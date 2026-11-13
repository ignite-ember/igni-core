"""Tests for :class:`DatabaseUrl` and :class:`ConflictInsert`.

``DatabaseUrl`` is the one key the engine registry, the migrator and
``igni migrate`` are keyed by: every spelling of one database must be one
value, the password must never reach a message, and anything but SQLite
or PostgreSQL is refused at the edge. ``ConflictInsert`` picks the
dialect's own ``insert`` so an upsert is one atomic statement.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, select
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session

from igni_core.core.db.upsert import ConflictInsert
from igni_core.core.db.urls import DatabaseUrl, Dialect

_table = Table(
    "kv",
    MetaData(),
    Column("k", String, primary_key=True),
    Column("v", Integer),
)


class TestDialect:
    def test_names_the_two_supported_databases(self):
        assert {d.value for d in Dialect} == {"sqlite", "postgresql"}


class TestDatabaseUrlParse:
    def test_the_sqlite_async_spelling_is_aiosqlite(self, tmp_path):
        url = DatabaseUrl.for_file(tmp_path / "s.db")
        assert url.async_ == f"sqlite+aiosqlite:///{(tmp_path / 's.db').resolve()}"
        assert url.sync == f"sqlite:///{(tmp_path / 's.db').resolve()}"
        assert url.is_sqlite
        assert url.dialect is Dialect.SQLITE

    def test_a_server_is_not_sqlite(self):
        url = DatabaseUrl.parse("postgres://u:p@h/db")
        assert not url.is_sqlite
        assert url.file is None

    @pytest.mark.parametrize("raw", ["sqlite://", "sqlite:///:memory:", "sqlite:///"])
    def test_in_memory_and_empty_sqlite_are_refused(self, raw):
        with pytest.raises(ValueError, match="in-memory SQLite"):
            DatabaseUrl.parse(raw)

    def test_another_dialect_is_refused_naming_it(self):
        with pytest.raises(ValueError, match="SQLite or PostgreSQL, not 'mysql'"):
            DatabaseUrl.parse("mysql+pymysql://u@h/db")

    def test_an_unparseable_url_is_a_value_error(self):
        with pytest.raises(ValueError, match="not a database URL"):
            DatabaseUrl.parse("://")

    def test_an_unparseable_url_does_not_echo_the_url(self):
        with pytest.raises(ValueError) as caught:
            DatabaseUrl.parse("not a url secretpw")
        assert "secretpw" not in str(caught.value)

    def test_a_special_character_password_round_trips(self):
        url = DatabaseUrl.parse("postgresql://u:p%40ss%2Fw%3Ard@h:5432/db")
        assert url.sync == "postgresql+psycopg://u:p%40ss%2Fw%3Ard@h:5432/db"
        assert url.redacted == "postgresql+psycopg://u:***@h:5432/db"

    def test_no_password_leaves_redacted_unchanged(self):
        url = DatabaseUrl.parse("postgresql://u@h/db")
        assert url.redacted == "postgresql+psycopg://u@h/db"
        assert str(url) == url.redacted

    def test_query_parameters_are_kept(self):
        url = DatabaseUrl.parse("postgresql://u:p@h/db?sslmode=require")
        assert url.sync.endswith("/db?sslmode=require")
        assert url.async_.endswith("/db?sslmode=require")

    def test_the_password_is_kept_in_the_stored_value_but_masked_in_str(self):
        url = DatabaseUrl.parse("postgresql://u:secret@h/db")
        assert url.url == "postgresql+psycopg://u:secret@h/db"
        assert str(url) == "postgresql+psycopg://u:***@h/db"

    def test_unicode_path_round_trips(self, tmp_path):
        path = tmp_path / "données.db"
        assert DatabaseUrl.for_file(path).file == path.resolve()


class TestDatabaseUrlPaths:
    def test_tilde_expands_to_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        assert DatabaseUrl.for_file("~/a/state.db").file == (tmp_path / "a" / "state.db").resolve()

    def test_a_relative_path_resolves_against_the_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert DatabaseUrl.for_file("x/../s.db").file == (tmp_path / "s.db").resolve()

    def test_a_symlinked_path_is_its_target(self, tmp_path):
        real = tmp_path / "real.db"
        real.touch()
        link = tmp_path / "link.db"
        link.symlink_to(real)
        assert DatabaseUrl.for_file(link) == DatabaseUrl.for_file(real)
        assert DatabaseUrl.for_file(link).file == real.resolve()

    def test_normalize_path_is_absolute(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert DatabaseUrl.normalize_path("a/../b") == str((tmp_path / "b").resolve())

    def test_of_returns_a_database_url_unchanged(self, tmp_path):
        url = DatabaseUrl.for_file(tmp_path / "s.db")
        assert DatabaseUrl.of(url) is url

    def test_of_treats_text_without_a_scheme_as_a_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert DatabaseUrl.of("").file == tmp_path.resolve()
        assert DatabaseUrl.of(str(tmp_path / "s.db")).file == (tmp_path / "s.db").resolve()

    def test_of_parses_text_containing_a_scheme(self):
        assert DatabaseUrl.of("postgres://u@h/db").dialect is Dialect.POSTGRESQL


class TestDatabaseUrlPrepare:
    def test_creates_nested_parents_and_is_idempotent(self, tmp_path):
        url = DatabaseUrl.for_file(tmp_path / "a" / "b" / "s.db")
        url.prepare()
        url.prepare()
        assert (tmp_path / "a" / "b").is_dir()
        assert not (tmp_path / "a" / "b" / "s.db").exists()

    def test_does_nothing_for_a_server(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        DatabaseUrl.parse("postgresql://u@h/db").prepare()
        assert list(tmp_path.iterdir()) == []


class TestDatabaseUrlValue:
    def test_is_immutable(self, tmp_path):
        url = DatabaseUrl.for_file(tmp_path / "s.db")
        with pytest.raises(ValidationError):
            url.url = "sqlite:///other.db"

    def test_is_hashable_and_a_registry_key(self, tmp_path):
        a = DatabaseUrl.for_file(tmp_path / "s.db")
        b = DatabaseUrl.of(f"sqlite:///{tmp_path}/x/../s.db")
        assert {a: 1}[b] == 1
        assert len({a, b}) == 1

    def test_an_unsupported_url_built_directly_refuses_on_dialect(self):
        with pytest.raises(ValueError, match="not 'mysql'"):
            DatabaseUrl(url="mysql://u@h/db").dialect  # noqa: B018


class TestConflictInsert:
    def test_sync_sqlite_session_gets_the_sqlite_insert(self):
        with Session(create_engine("sqlite://")) as session:
            stmt = ConflictInsert.into(session, _table)
        assert isinstance(stmt, sqlite.Insert)
        assert hasattr(stmt, "on_conflict_do_update")

    async def test_async_session_is_resolved_through_bind(self):
        engine = create_async_engine("sqlite+aiosqlite://")
        try:
            async with AsyncSession(engine) as session:
                stmt = ConflictInsert.into(session, _table)
            assert isinstance(stmt, sqlite.Insert)
        finally:
            await engine.dispose()

    def test_an_unsupported_dialect_is_refused_naming_it(self):
        class Bind:
            class dialect:  # noqa: N801
                name = "mysql"

        class Fake(Session):
            def get_bind(self, *args, **kwargs):
                return Bind()

        with pytest.raises(ValueError, match="'mysql'") as caught:
            ConflictInsert.into(Fake(), _table)
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__

    def test_the_upsert_is_one_statement_that_updates_on_conflict(self):
        engine = create_engine("sqlite://")
        _table.metadata.create_all(engine)
        with Session(engine) as session:
            for value in (1, 2):
                stmt = ConflictInsert.into(session, _table).values(k="a", v=value)
                session.execute(
                    stmt.on_conflict_do_update(index_elements=["k"], set_={"v": stmt.excluded.v})
                )
            ignore = ConflictInsert.into(session, _table).values(k="a", v=9)
            session.execute(ignore.on_conflict_do_nothing(index_elements=["k"]))
            assert session.execute(select(_table.c.k, _table.c.v)).all() == [("a", 2)]
