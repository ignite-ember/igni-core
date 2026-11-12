"""``igni migrate`` says, per database, what it did: the wording, the targets, the fallbacks.

``test_storage_backends.py`` pins the named-database path on every backend.
This pins the report itself: a new database is ``new -> HEAD``, a current one
is ``already at HEAD`` (and a second run changes nothing), an old one names the
revision it came from, with no URL the project's and the global database are
both migrated (once when they are one file), and ``IGNI_DATABASE_URL`` and
``storage.database_url`` name the database as ``--database-url`` does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine, text

from igni_core.cli.migrate import SchemaMigration, migrate
from igni_core.core.data_paths import canonical_project_dir
from igni_core.core.db.locator import StorageLocator
from igni_core.core.db.schema import HEAD_REVISION, SchemaRevision
from igni_core.core.db.urls import DatabaseUrl


def _run(*args: str):
    return CliRunner().invoke(migrate, list(args))


def _reports(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.strip()]


def test_a_new_database_is_reported_as_new_and_a_second_run_as_current(tmp_path: Path):
    db = tmp_path / "state.db"
    url = DatabaseUrl.for_file(db)

    first = _run("--database-url", url.url, "--project-dir", str(tmp_path))
    second = _run("--database-url", url.url, "--project-dir", str(tmp_path))

    assert first.exit_code == 0, first.output
    assert _reports(first.output) == [f"{db.resolve()}: new -> {HEAD_REVISION}"]
    assert second.exit_code == 0, second.output
    assert _reports(second.output) == [f"{db.resolve()}: already at {HEAD_REVISION}"]
    assert SchemaRevision.of(url) == HEAD_REVISION


def test_a_database_behind_head_names_the_revision_it_came_from(tmp_path: Path):
    url = DatabaseUrl.for_file(tmp_path / "state.db")
    assert _run("--database-url", url.url, "--project-dir", str(tmp_path)).exit_code == 0
    engine = create_engine(url.url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = 'behind'"))
    engine.dispose()
    assert SchemaRevision.of(url) == "behind"

    result = _run("--database-url", url.url, "--project-dir", str(tmp_path))

    assert result.exit_code == 0, result.output
    assert _reports(result.output) == [
        f"{tmp_path.resolve() / 'state.db'}: behind -> {HEAD_REVISION}"
    ]


def test_with_no_url_the_project_and_the_global_database_are_both_migrated(tmp_path: Path):
    data = tmp_path / "data"
    project = tmp_path / "proj"
    project.mkdir()

    result = _run("--data-dir", str(data), "--project-dir", str(project))

    assert result.exit_code == 0, result.output
    lines = _reports(result.output)
    assert len(lines) == 2
    assert all(line.endswith(f": new -> {HEAD_REVISION}") for line in lines)
    assert lines[0].split(":")[0] != lines[1].split(":")[0]
    assert lines[1].startswith(str(data.resolve()))
    assert lines[1].split(":")[0].endswith("igni.db")
    assert str(data.resolve()) in lines[0]


def test_one_database_shared_by_project_and_global_is_migrated_once(tmp_path: Path):
    url = DatabaseUrl.for_file(tmp_path / "one.db")
    locator = StorageLocator(database_url=url.url, data_dir=tmp_path)
    migration = SchemaMigration(locator, canonical_project_dir(tmp_path))

    assert migration.targets() == [url]


def test_separate_project_and_global_databases_are_two_targets(tmp_path: Path):
    locator = StorageLocator(database_url=None, data_dir=tmp_path / "data")
    migration = SchemaMigration(locator, canonical_project_dir(tmp_path))

    targets = migration.targets()

    assert targets == [locator.project_url(canonical_project_dir(tmp_path)), locator.global_url()]
    assert targets[0] != targets[1]


def test_the_environment_names_the_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "from-env.db"
    monkeypatch.setenv("IGNI_DATABASE_URL", DatabaseUrl.for_file(db).url)

    result = _run("--project-dir", str(tmp_path))

    assert result.exit_code == 0, result.output
    assert _reports(result.output) == [f"{db.resolve()}: new -> {HEAD_REVISION}"]


def test_the_flag_beats_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("IGNI_DATABASE_URL", DatabaseUrl.for_file(tmp_path / "env.db").url)
    flagged = tmp_path / "flag.db"

    result = _run(
        "--database-url", DatabaseUrl.for_file(flagged).url, "--project-dir", str(tmp_path)
    )

    assert _reports(result.output) == [f"{flagged.resolve()}: new -> {HEAD_REVISION}"]
    assert not (tmp_path / "env.db").exists()


def test_a_database_that_cannot_be_reached_is_a_message_and_exit_one(tmp_path: Path):
    unreachable = "postgresql+psycopg://nobody:hunter2@127.0.0.1:1/none"

    result = _run("--database-url", unreachable, "--project-dir", str(tmp_path))

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), "a message, not a traceback"
    assert "could not migrate postgresql+psycopg://nobody:***@127.0.0.1:1/none" in result.output
    assert "Connection refused" in result.output
    assert "hunter2" not in result.output


def test_a_project_dir_that_is_a_file_is_refused(tmp_path: Path):
    a_file = tmp_path / "file"
    a_file.write_text("x")

    result = _run("--project-dir", str(a_file))

    assert result.exit_code == 2
    assert "is a file" in result.output
