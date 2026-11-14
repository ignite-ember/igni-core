"""FileIndex's edges: git that is missing, slow or empty, the walk's ignore
list, reloading, and a limit that is zero or negative."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from igni_core.core.utils import file_index as file_index_module
from igni_core.core.utils.file_index import FileIndex, _fallback_list, _git_ls_files


def _touch(root: Path, *names: str) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


class TestGitListing:
    @pytest.mark.parametrize(
        "failure",
        [subprocess.TimeoutExpired("git", 10), FileNotFoundError("git"), PermissionError("git")],
    )
    def test_a_git_that_cannot_run_lists_nothing(self, tmp_path, monkeypatch, failure):
        def boom(*args, **kwargs):
            raise failure

        monkeypatch.setattr(file_index_module.subprocess, "run", boom)

        assert _git_ls_files(tmp_path) == []

    def test_git_is_given_ten_seconds(self, tmp_path, monkeypatch):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout="b\na\n", stderr="")

        monkeypatch.setattr(file_index_module.subprocess, "run", fake_run)

        assert _git_ls_files(tmp_path) == ["a", "b"]
        assert seen["timeout"] == 10
        assert seen["cwd"] == tmp_path

    def test_tracked_and_untracked_files_are_listed_but_ignored_ones_are_not(self, tmp_path):
        _git(tmp_path, "init")
        _touch(tmp_path, "tracked.py", "new.py", "skip.log", ".gitignore", "pkg/inner.py")
        (tmp_path / ".gitignore").write_text("*.log\n")
        _git(tmp_path, "add", "tracked.py")

        assert _git_ls_files(tmp_path) == [".gitignore", "new.py", "pkg/inner.py", "tracked.py"]

    def test_an_empty_repository_lists_nothing(self, tmp_path):
        _git(tmp_path, "init")

        assert _git_ls_files(tmp_path) == []


class TestTheWalk:
    def test_every_ignored_directory_is_skipped_and_directories_are_not_listed(self, tmp_path):
        _touch(
            tmp_path,
            "keep.py",
            "src/keep.py",
            ".git/config",
            ".hg/x",
            ".svn/x",
            ".venv/x",
            "venv/x",
            ".env/x",
            ".tox/x",
            ".mypy_cache/x",
            ".pytest_cache/x",
            ".ruff_cache/x",
            "dist/x",
            "build/x",
            ".eggs/x",
            "pkg.egg-info/PKG-INFO",
            "a/b/node_modules/x",
        )

        assert _fallback_list(tmp_path) == ["keep.py", "src/keep.py"]

    def test_a_file_named_like_an_ignored_directory_is_skipped_too(self, tmp_path):
        _touch(tmp_path, ".env", "ok.txt")

        assert _fallback_list(tmp_path) == ["ok.txt"]

    def test_a_dangling_symlink_is_not_listed(self, tmp_path):
        _touch(tmp_path, "real.txt")
        (tmp_path / "dangling").symlink_to(tmp_path / "missing")

        assert _fallback_list(tmp_path) == ["real.txt"]


class TestLoading:
    @pytest.mark.asyncio
    async def test_a_git_repository_with_no_files_falls_back_to_the_walk(self, tmp_path):
        _git(tmp_path, "init")
        _touch(tmp_path, "a.txt", "b.txt")
        (tmp_path / ".git" / "info" / "exclude").write_text("*\n")
        assert _git_ls_files(tmp_path) == []
        index = FileIndex(tmp_path)

        await index.ensure_loaded()

        assert index.is_loaded
        assert index.match("") == ["a.txt", "b.txt"]

    @pytest.mark.asyncio
    async def test_a_directory_that_is_not_a_repository_is_walked(self, tmp_path):
        _touch(tmp_path, "b.py", "a/c.py")
        index = FileIndex(tmp_path)

        await index.ensure_loaded()

        assert index.match("") == ["a/c.py", "b.py"]

    def test_the_index_is_stale_until_refreshed(self, tmp_path):
        _touch(tmp_path, "one.py")
        index = FileIndex(tmp_path)
        index.refresh_sync()
        _touch(tmp_path, "two.py")

        assert index.match("") == ["one.py"]

        index.refresh_sync()

        assert index.match("") == ["one.py", "two.py"]
        assert index.is_loaded

    def test_a_refresh_prefers_git_and_falls_back_to_the_walk(self, tmp_path):
        _touch(tmp_path, "walk.py")
        index = FileIndex(tmp_path)

        index.refresh_sync()

        assert index.match("") == ["walk.py"]

    def test_an_index_without_a_project_reads_the_working_directory(self, tmp_path, monkeypatch):
        _touch(tmp_path, "here.py")
        monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
        index = FileIndex()

        index.refresh_sync()

        assert index.match("") == ["here.py"]

    def test_an_index_never_loaded_is_not_loaded_and_empty(self, tmp_path):
        index = FileIndex(tmp_path)

        assert not index.is_loaded
        assert index.file_count == 0
        assert index.match_with_total("x") == ([], 0)


class TestMatching:
    def _index(self, files: list[str]) -> FileIndex:
        index = FileIndex(Path("."))
        index._files = files
        index._loaded = True
        return index

    def test_the_total_counts_matches_before_the_limit_cuts_them(self):
        index = self._index(["a1.py", "a2.py", "a3.py", "b.py"])

        matches, total = index.match_with_total("a", limit=2)

        assert len(matches) == 2
        assert total == 3

    def test_an_empty_query_returns_the_first_files_and_the_whole_count(self):
        index = self._index(["a", "b", "c"])

        assert index.match_with_total("", limit=2) == (["a", "b"], 3)

    def test_a_query_nothing_matches_has_a_total_of_zero(self):
        assert self._index(["a.py"]).match_with_total("zzz") == ([], 0)

    def test_the_better_match_comes_first(self):
        index = self._index(["src/a/b/c/readme_notes.txt", "README.md"])

        assert index.match("readme")[0] == "README.md"

    @pytest.mark.parametrize("query", ["", "a"])
    @pytest.mark.parametrize("limit", [0, -1, -5])
    def test_a_limit_of_nothing_returns_nothing_but_still_counts(self, query, limit):
        index = self._index(["a", "ab", "b"])

        matches, total = index.match_with_total(query, limit=limit)

        assert matches == []
        assert total == (3 if query == "" else 2)

    def test_a_query_longer_than_every_path_matches_nothing(self):
        assert self._index(["a.py"]).match("a.pyxxxxxxxxxxxxx") == []
