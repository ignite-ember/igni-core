"""Where a project's data lives: one id per project, whatever git does, and no lost files.

``core/data_paths.py``. The id names the project's data directory, so it
must not change between calls, must not depend on how the path was
spelt, and must fall back to the path when git cannot answer.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path

import pytest

from igni_core.core import data_paths
from igni_core.core.data_paths import (
    ProjectIds,
    canonical_project_dir,
    data_root,
    global_db_path,
    project_data_dir,
    resolve_project_id,
    state_db_path,
)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _fake_git(directory: Path, script: str, monkeypatch) -> None:
    """A ``git`` first on ``PATH`` that runs ``script`` instead."""
    directory.mkdir(parents=True, exist_ok=True)
    git = directory / "git"
    git.write_text("#!/bin/sh\n" + script + "\n")
    git.chmod(git.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")


class TestTheCanonicalDirectory:
    def test_a_path_that_does_not_exist_still_has_a_name(self, tmp_path: Path):
        ghost = tmp_path / "not" / "here"
        assert canonical_project_dir(ghost) == tmp_path.resolve() / "not" / "here"

    def test_the_home_marker_is_expanded(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert canonical_project_dir("~/work") == tmp_path.resolve() / "work"

    def test_a_symlink_a_relative_path_and_a_trailing_slash_name_one_project(
        self, tmp_path: Path, monkeypatch
    ):
        real = tmp_path / "real"
        real.mkdir()
        (tmp_path / "link").symlink_to(real)
        monkeypatch.chdir(tmp_path)
        for spelling in [real, tmp_path / "link", Path("link"), f"{real}/"]:
            assert canonical_project_dir(spelling) == real.resolve()
        assert canonical_project_dir(real / "." / "..") == tmp_path.resolve()

    def test_the_data_root_expands_the_home_marker(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert data_root("~/.igni-data") == tmp_path / ".igni-data"
        assert data_root(tmp_path / "x") == tmp_path / "x"


class TestTheProjectId:
    def test_it_is_sixteen_hex_characters(self, tmp_path: Path):
        project_id = ProjectIds().of(tmp_path)
        assert len(project_id) == 16 and int(project_id, 16) >= 0

    def test_two_clones_of_one_origin_share_it_and_a_different_origin_does_not(
        self, tmp_path: Path
    ):
        def clone(name: str, remote: str) -> Path:
            path = tmp_path / name
            path.mkdir()
            subprocess.run(["git", "init", "-q", str(path)], check=True)
            subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)
            return path

        ids = ProjectIds()
        a = clone("a", "https://example.test/one.git")
        b = clone("b", "https://example.test/one.git")
        c = clone("c", "https://example.test/two.git")
        assert ids.of(a) == ids.of(b) == _hash("https://example.test/one.git")
        assert ids.of(c) != ids.of(a)

    def test_a_directory_with_no_remote_is_named_by_its_path(self, tmp_path: Path):
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        assert ProjectIds().of(tmp_path) == _hash(str(tmp_path.resolve()))

    def test_a_missing_git_falls_back_to_the_path(self, tmp_path: Path, monkeypatch):
        empty = tmp_path / "nothing-on-the-path"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        project = tmp_path / "p"
        project.mkdir()
        assert ProjectIds().of(project) == _hash(str(project.resolve()))

    def test_a_git_that_hangs_falls_back_to_the_path_after_its_timeout(
        self, tmp_path: Path, monkeypatch
    ):
        _fake_git(tmp_path / "bin", "sleep 3; echo https://example.test/late.git", monkeypatch)
        monkeypatch.setattr(data_paths, "_GIT_TIMEOUT_SECONDS", 0.3)
        project = tmp_path / "p"
        project.mkdir()
        assert ProjectIds().of(project) == _hash(str(project.resolve()))

    def test_a_git_that_prints_nothing_falls_back_to_the_path(self, tmp_path: Path, monkeypatch):
        _fake_git(tmp_path / "bin", "exit 0", monkeypatch)
        project = tmp_path / "p"
        project.mkdir()
        assert ProjectIds().of(project) == _hash(str(project.resolve()))

    def test_a_git_that_fails_falls_back_to_the_path_whatever_it_printed(
        self, tmp_path: Path, monkeypatch
    ):
        _fake_git(tmp_path / "bin", "echo https://example.test/x.git; exit 2", monkeypatch)
        project = tmp_path / "p"
        project.mkdir()
        assert ProjectIds().of(project) == _hash(str(project.resolve()))

    def test_clear_makes_the_next_ask_go_to_git_again(self, tmp_path: Path, monkeypatch):
        project = tmp_path / "p"
        project.mkdir()
        _fake_git(tmp_path / "bin", "echo https://example.test/first.git", monkeypatch)
        ids = ProjectIds()
        assert ids.of(project) == _hash("https://example.test/first.git")
        _fake_git(tmp_path / "bin", "echo https://example.test/second.git", monkeypatch)
        # Not a repository, so nothing marks a change: the answer is kept.
        assert ids.of(project) == _hash("https://example.test/first.git")
        ids.clear()
        assert ids.of(project) == _hash("https://example.test/second.git")

    def test_the_module_function_uses_the_process_cache(self, tmp_path: Path):
        assert resolve_project_id(tmp_path) == data_paths.project_ids.of(tmp_path)


class TestThePaths:
    def test_a_project_s_directory_and_state_database_hang_off_the_data_root(self, tmp_path: Path):
        project = tmp_path / "proj"
        project.mkdir()
        root = tmp_path / "data"
        project_id = resolve_project_id(project)
        assert project_data_dir(project, data_dir=root) == root / "projects" / project_id
        assert state_db_path(project, data_dir=root) == root / "projects" / project_id / "state.db"

    def test_two_spellings_of_a_project_share_one_state_database(self, tmp_path: Path):
        project = tmp_path / "proj"
        project.mkdir()
        (tmp_path / "alias").symlink_to(project)
        root = tmp_path / "data"
        assert state_db_path(tmp_path / "alias", data_dir=root) == state_db_path(
            f"{project}/", data_dir=root
        )


class TestTheGlobalDatabaseMove:
    def test_a_move_that_fails_part_way_loses_nothing(self, tmp_path: Path, monkeypatch, caplog):
        """The main file moved and its write-ahead log did not: igni.db would open
        without the rows that were only in the log, so the move is undone."""
        (tmp_path / "ember.db").write_bytes(b"main")
        (tmp_path / "ember.db-wal").write_bytes(b"wal")
        real_replace = os.replace

        def fail_on_the_log(src: object, dst: object) -> None:
            if str(src).endswith("-wal"):
                raise PermissionError("read-only")
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", fail_on_the_log)
        assert global_db_path(tmp_path) == tmp_path / "ember.db"
        assert (tmp_path / "ember.db").read_bytes() == b"main"
        assert (tmp_path / "ember.db-wal").read_bytes() == b"wal"
        assert not (tmp_path / "igni.db").exists()
        assert "could not move" in caplog.text

    def test_a_data_root_with_a_home_marker_is_expanded_first(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "d").mkdir()
        (tmp_path / "d" / "ember.db").write_bytes(b"x")
        assert global_db_path("~/d") == tmp_path / "d" / "igni.db"
        assert (tmp_path / "d" / "igni.db").read_bytes() == b"x"


@pytest.fixture(autouse=True)
def _a_home_of_its_own(tmp_path_factory, monkeypatch):
    # ``~`` in these tests must never mean the person running them.
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
