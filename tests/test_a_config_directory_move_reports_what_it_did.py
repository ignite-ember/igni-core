"""What the ``.ember`` to ``.igni`` move says it did, and how loudly.

``core/dir_migration.py``. ``test_dir_migration.py`` pins what it moves
and refuses; these pin what it reports, on real directories.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from igni_core.core.dir_migration import migrate_directory, migrate_home, migrate_project
from igni_core.core.paths import CONFIG_DIR, LEGACY_CONFIG_DIR

LOGGER = "igni_core.core.dir_migration"


def _legacy(parent: Path) -> Path:
    directory = parent / LEGACY_CONFIG_DIR
    directory.mkdir(parents=True)
    (directory / "credentials.json").write_text('{"token": "t"}')
    return directory


def test_a_move_names_where_from_and_where_to(tmp_path: Path):
    _legacy(tmp_path)
    outcome = migrate_directory(tmp_path)
    assert outcome.moved and outcome.reason is None
    assert outcome.source == tmp_path / LEGACY_CONFIG_DIR
    assert outcome.destination == tmp_path / CONFIG_DIR


def test_a_refusal_names_no_paths_and_says_why(tmp_path: Path):
    outcome = migrate_directory(tmp_path)
    assert (outcome.moved, outcome.reason) == (False, "no legacy directory")
    assert outcome.source is None and outcome.destination is None


def test_two_directories_are_both_named_and_both_kept(tmp_path: Path):
    _legacy(tmp_path)
    (tmp_path / CONFIG_DIR).mkdir()
    outcome = migrate_directory(tmp_path)
    assert not outcome.moved
    assert CONFIG_DIR in (outcome.reason or "") and LEGACY_CONFIG_DIR in (outcome.reason or "")
    assert (tmp_path / LEGACY_CONFIG_DIR / "credentials.json").exists()


def test_a_dangling_legacy_link_is_not_followed_or_removed(tmp_path: Path):
    link = tmp_path / LEGACY_CONFIG_DIR
    link.symlink_to(tmp_path / "nowhere")
    outcome = migrate_directory(tmp_path)
    assert not outcome.moved
    assert link.is_symlink() and not (tmp_path / CONFIG_DIR).exists()


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs a parent that refuses")
def test_a_parent_that_refuses_the_rename_is_a_reason_not_an_exception(tmp_path: Path):
    parent = tmp_path / "parent"
    parent.mkdir()
    _legacy(parent)
    parent.chmod(0o500)
    try:
        outcome = migrate_directory(parent)
    finally:
        parent.chmod(0o700)
    assert not outcome.moved
    assert (outcome.reason or "").startswith("could not rename")
    assert (parent / LEGACY_CONFIG_DIR / "credentials.json").read_text() == '{"token": "t"}'


class TestWhatIsLogged:
    def test_a_project_move_is_logged_at_info_and_says_project(self, tmp_path: Path, caplog):
        _legacy(tmp_path)
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            migrate_project(tmp_path)
        (record,) = caplog.records
        assert record.levelno == logging.INFO
        assert "Project config directory renamed" in record.getMessage()

    def test_a_project_with_nothing_to_move_is_only_debug(self, tmp_path: Path, caplog):
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            migrate_project(tmp_path)
        (record,) = caplog.records
        assert record.levelno == logging.DEBUG
        assert "no legacy directory" in record.getMessage()

    def test_a_home_move_is_logged_at_info_and_says_config(
        self, tmp_path: Path, caplog, monkeypatch
    ):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        _legacy(tmp_path)
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            migrate_home()
        (record,) = caplog.records
        assert record.levelno == logging.INFO
        assert record.getMessage().startswith("Config directory renamed")

    def test_a_home_with_nothing_to_move_is_only_debug(self, tmp_path: Path, caplog, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            migrate_home()
        (record,) = caplog.records
        assert record.levelno == logging.DEBUG
        assert record.getMessage().startswith("Home config directory")


def test_a_project_directory_given_as_text_is_moved(tmp_path: Path):
    _legacy(tmp_path)
    assert migrate_project(str(tmp_path)).moved  # type: ignore[arg-type]
    assert (tmp_path / CONFIG_DIR / "credentials.json").exists()
