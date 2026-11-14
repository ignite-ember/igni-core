"""What :class:`Migrator` does around ``alembic upgrade head``.

The real package's migrations are run elsewhere (the schema keeper's tests);
here a tiny package of one migration stands in, so that the failing,
repeated and concurrent runs can be made on purpose.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from igni_core.core.db import migrations
from igni_core.core.db.migrations import Migrator, SchemaTooNew, upgrade_to_head
from igni_core.core.db.urls import DatabaseUrl

INI = """\
[alembic]
script_location = %(here)s/migrations
sqlalchemy.url = sqlite:///unused.db
"""

ENV = """\
from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
connectable = engine_from_config(
    config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool
)
with connectable.connect() as connection:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
"""

MAKO = """\
\"\"\"${message}\"\"\"
revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
"""

CREATES_A_TABLE = """\
revision = "0001"
down_revision = None

from alembic import op


def upgrade():
    op.execute("CREATE TABLE made_by_the_migration (n INTEGER)")


def downgrade():
    pass
"""

FAILS = """\
revision = "0001"
down_revision = None


def upgrade():
    raise RuntimeError("the migration script is broken")


def downgrade():
    pass
"""


def package(root: Path, migration: str = CREATES_A_TABLE) -> Path:
    (root / "migrations" / "versions").mkdir(parents=True, exist_ok=True)
    (root / "alembic.ini").write_text(INI)
    (root / "migrations" / "env.py").write_text(ENV)
    (root / "migrations" / "script.py.mako").write_text(MAKO)
    (root / "migrations" / "versions" / "0001_first.py").write_text(migration)
    return root


def tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}


def test_a_new_file_is_upgraded_to_the_migrations_head(tmp_path: Path):
    path = tmp_path / "data" / "state.db"
    Migrator(package(tmp_path / "pkg")).upgrade_to_head(path)
    assert {"made_by_the_migration", "alembic_version"} <= tables(path)


def test_a_database_is_upgraded_once_per_process_and_the_second_call_is_free(tmp_path: Path):
    path = tmp_path / "state.db"
    migrator = Migrator(package(tmp_path / "pkg"))
    migrator.upgrade_to_head(path)
    path.unlink()
    migrator.upgrade_to_head(path)
    assert not path.exists()
    # Another spelling of the same file is the same database.
    migrator.upgrade_to_head(tmp_path / "." / "state.db")
    assert not path.exists()
    # Another migrator keeps its own record.
    Migrator(package(tmp_path / "pkg2")).upgrade_to_head(path)
    assert path.exists()


def test_concurrent_callers_on_one_database_run_the_upgrade_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    runs: list[str] = []
    real_upgrade = migrations.command.upgrade

    def counting(config: Any, revision: str) -> None:
        runs.append(revision)
        real_upgrade(config, revision)

    monkeypatch.setattr(migrations.command, "upgrade", counting)
    path = tmp_path / "state.db"
    migrator = Migrator(package(tmp_path / "pkg"))
    start = threading.Barrier(6)
    failures: list[BaseException] = []

    def upgrade() -> None:
        start.wait()
        try:
            migrator.upgrade_to_head(path)
        except BaseException as failure:  # noqa: BLE001
            failures.append(failure)

    threads = [threading.Thread(target=upgrade) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    assert runs == ["head"]


def test_a_failing_migration_reaches_the_caller_and_the_next_call_tries_again(tmp_path: Path):
    path = tmp_path / "state.db"
    root = package(tmp_path / "pkg", FAILS)
    migrator = Migrator(root)
    with pytest.raises(RuntimeError, match="broken"):
        migrator.upgrade_to_head(path)
    package(root, CREATES_A_TABLE)
    migrator.upgrade_to_head(path)
    assert "made_by_the_migration" in tables(path)


def test_a_package_without_its_migrations_says_to_reinstall_but_only_when_one_is_needed(
    tmp_path: Path,
):
    empty = tmp_path / "empty"
    empty.mkdir()
    migrator = Migrator(empty)  # constructing it is free
    with pytest.raises(FileNotFoundError, match="reinstall") as refusal:
        migrator.upgrade_to_head(tmp_path / "state.db")
    assert str(empty) in str(refusal.value)
    assert "pip install --force-reinstall igni-core" in str(refusal.value)


def test_a_package_with_the_ini_but_no_migrations_directory_is_refused_too(tmp_path: Path):
    root = tmp_path / "half"
    root.mkdir()
    (root / "alembic.ini").write_text(INI)
    with pytest.raises(FileNotFoundError, match="reinstall"):
        Migrator(root).upgrade_to_head(tmp_path / "state.db")


def test_a_password_with_a_percent_reaches_alembic_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    seen: dict[str, Any] = {}

    def recording(config: Any, revision: str) -> None:
        seen["url"] = config.get_main_option("sqlalchemy.url")
        seen["configure_logging"] = config.attributes["configure_logging"]
        seen["revision"] = revision

    monkeypatch.setattr(migrations.command, "upgrade", recording)
    url = DatabaseUrl.parse("postgresql://igni:p%25ss%2Fw@db.example:5432/igni")
    Migrator(package(tmp_path / "pkg")).upgrade_to_head(url)
    assert seen["url"] == url.sync
    assert seen == {"url": url.sync, "configure_logging": False, "revision": "head"}


def test_an_in_process_upgrade_keeps_the_applications_logging(tmp_path: Path):
    root_logger = logging.getLogger()
    handlers_before = list(root_logger.handlers)
    Migrator(package(tmp_path / "pkg")).upgrade_to_head(tmp_path / "state.db")
    assert list(root_logger.handlers) == handlers_before


def test_the_module_level_upgrade_goes_through_whichever_migrator_is_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(migrations, "migrator", Migrator(empty))
    with pytest.raises(FileNotFoundError, match="reinstall"):
        upgrade_to_head(tmp_path / "state.db")
    monkeypatch.setattr(migrations, "migrator", Migrator(package(tmp_path / "pkg")))
    upgrade_to_head(tmp_path / "state.db")
    assert "made_by_the_migration" in tables(tmp_path / "state.db")


def test_a_database_a_newer_igni_wrote_says_so_instead_of_alembics_own_words(tmp_path: Path):
    path = tmp_path / "state.db"
    root = package(tmp_path / "pkg")
    migrator = Migrator(root)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        connection.execute("INSERT INTO alembic_version VALUES ('ffffnewer')")
    with pytest.raises(SchemaTooNew, match="newer igni") as refusal:
        migrator.upgrade_to_head(path)
    assert "ffffnewer" in str(refusal.value)
    assert "Can't locate" not in str(refusal.value)
    # Not remembered as upgraded: a retry asks again.
    with pytest.raises(SchemaTooNew):
        migrator.upgrade_to_head(path)
