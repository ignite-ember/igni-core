"""Tests for worktree.py — git worktree lifecycle management."""

import subprocess
from pathlib import Path

import pytest

from igni_core.core.worktree import WorktreeManager, WorktreeRoot


def _init_git_repo(path: Path) -> None:
    """Initialize a git repo with an initial commit."""
    subprocess.run(["git", "init"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, capture_output=True)
    (path / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True)


class TestWorktreeManager:
    def test_validates_git_repo(self, tmp_path):
        with pytest.raises(RuntimeError, match="Not a git repository"):
            WorktreeManager(tmp_path)

    def test_creates_worktree(self, tmp_path):
        _init_git_repo(tmp_path)
        wm = WorktreeManager(tmp_path)
        info = wm.create(branch_name="test-branch")

        assert info.worktree_path.exists()
        assert info.branch_name == "test-branch"
        assert info.original_dir == tmp_path.resolve()
        assert (info.worktree_path / "README.md").exists()

        # Cleanup
        wm.cleanup()

    def test_auto_generates_branch_name(self, tmp_path):
        _init_git_repo(tmp_path)
        wm = WorktreeManager(tmp_path)
        info = wm.create(session_id="abc123")

        assert info.branch_name == "igni-worktree-abc123"
        assert info.worktree_path.exists()

        wm.cleanup()

    def test_has_changes_false_on_clean(self, tmp_path):
        _init_git_repo(tmp_path)
        wm = WorktreeManager(tmp_path)
        wm.create(branch_name="clean-test")

        assert not wm.has_changes()
        wm.cleanup()

    def test_has_changes_true_on_modified(self, tmp_path):
        _init_git_repo(tmp_path)
        wm = WorktreeManager(tmp_path)
        info = wm.create(branch_name="dirty-test")

        # Make a change in the worktree
        (info.worktree_path / "new_file.txt").write_text("hello")
        assert wm.has_changes()

        # Force cleanup for test teardown
        subprocess.run(
            ["git", "worktree", "remove", str(info.worktree_path), "--force"],
            cwd=tmp_path,
            capture_output=True,
        )
        subprocess.run(
            ["git", "branch", "-D", "dirty-test"],
            cwd=tmp_path,
            capture_output=True,
        )

    def test_cleanup_removes_clean_worktree(self, tmp_path):
        _init_git_repo(tmp_path)
        wm = WorktreeManager(tmp_path)
        info = wm.create(branch_name="cleanup-test")

        assert info.worktree_path.exists()
        cleaned = wm.cleanup()
        assert cleaned.ok
        assert wm.info is None

    def test_cleanup_preserves_dirty_worktree(self, tmp_path):
        _init_git_repo(tmp_path)
        wm = WorktreeManager(tmp_path)
        info = wm.create(branch_name="preserve-test")

        (info.worktree_path / "change.txt").write_text("data")
        cleaned = wm.cleanup()
        assert cleaned.status == "preserved_dirty"
        assert info.worktree_path.exists()

        # Force cleanup for test teardown
        subprocess.run(
            ["git", "worktree", "remove", str(info.worktree_path), "--force"],
            cwd=tmp_path,
            capture_output=True,
        )
        subprocess.run(
            ["git", "branch", "-D", "preserve-test"],
            cwd=tmp_path,
            capture_output=True,
        )

    def test_error_on_duplicate_worktree(self, tmp_path):
        _init_git_repo(tmp_path)
        wm = WorktreeManager(tmp_path)
        wm.create(branch_name="dup-test")

        wm2 = WorktreeManager(tmp_path)
        with pytest.raises(RuntimeError, match="already exists"):
            wm2.create(branch_name="dup-test")

        wm.cleanup()


class TestCleanupStaleWorktrees:
    def test_prunes_stale_worktrees(self, tmp_path):
        _init_git_repo(tmp_path)
        # Just verify it doesn't crash on a clean repo
        result = WorktreeRoot().prune_stale(tmp_path)
        assert isinstance(result.empty_dirs_removed, list)


def _manager(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    root = WorktreeRoot(tmp_path / "wt-root")
    return repo, root, root.manager_for(repo)


class TestNotARepository:
    def test_a_missing_repo_dir_is_not_a_git_repository(self, tmp_path):
        with pytest.raises(RuntimeError, match="Not a git repository"):
            WorktreeManager(tmp_path / "missing")

    def test_a_file_as_repo_dir_is_not_a_git_repository(self, tmp_path):
        f = tmp_path / "file"
        f.write_text("x")
        with pytest.raises(RuntimeError, match="Not a git repository"):
            WorktreeManager(f)


class TestWorktreeRoot:
    def test_default_root_is_under_the_config_dir(self):
        from igni_core.core.paths import CONFIG_DIR

        assert WorktreeRoot().root == Path.home() / CONFIG_DIR / "worktrees"

    def test_ensure_exists_is_idempotent_and_path_for_joins(self, tmp_path):
        root = WorktreeRoot(tmp_path / "a" / "b")
        root.ensure_exists()
        root.ensure_exists()
        assert (tmp_path / "a" / "b").is_dir()
        assert root.path_for("x") == tmp_path / "a" / "b" / "x"

    def test_manager_for_binds_this_root(self, tmp_path):
        repo, root, wm = _manager(tmp_path)
        assert wm.root is root
        assert wm.create(branch_name="bound").worktree_path == root.root / "bound"
        wm.cleanup()

    def test_prune_stale_without_a_root_does_not_touch_git(self, tmp_path):
        result = WorktreeRoot(tmp_path / "absent").prune_stale(tmp_path / "not-a-repo")
        assert result.pruned_by_git is False
        assert result.warnings == []
        assert result.empty_dirs_removed == []

    def test_prune_stale_removes_only_empty_dirs(self, tmp_path):
        repo, root, _ = _manager(tmp_path)
        root.ensure_exists()
        (root.root / "empty").mkdir()
        (root.root / "full").mkdir()
        (root.root / "full" / "f").write_text("x")
        (root.root / "plain-file").write_text("x")
        result = root.prune_stale(repo)
        assert result.pruned_by_git is True
        assert result.warnings == []
        assert result.empty_dirs_removed == ["empty"]
        assert not (root.root / "empty").exists()
        assert (root.root / "full" / "f").exists()
        assert (root.root / "plain-file").exists()

    def test_prune_stale_forgets_a_worktree_whose_directory_vanished(self, tmp_path):
        import shutil

        repo, root, wm = _manager(tmp_path)
        info = wm.create(branch_name="gone")
        shutil.rmtree(info.worktree_path)
        listing = subprocess.run(
            ["git", "worktree", "list"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert "gone" in listing
        root.prune_stale(repo)
        listing = subprocess.run(
            ["git", "worktree", "list"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert "gone" not in listing

    def test_prune_stale_outside_a_repo_reports_failure_and_stderr(self, tmp_path):
        root = WorktreeRoot(tmp_path / "r")
        root.ensure_exists()
        outside = tmp_path / "plain"
        outside.mkdir()
        result = root.prune_stale(outside)
        assert result.pruned_by_git is False
        assert result.warnings and "not a git repository" in result.warnings[0].lower()


class TestCreateResult:
    def test_success_populates_info(self, tmp_path):
        repo, root, wm = _manager(tmp_path)
        result = wm.create_result(branch_name="ok-branch")
        assert result.ok and result.error == "" and result.message == ""
        assert result.info == wm.info
        assert result.info.worktree_path == root.root / "ok-branch"
        assert result.info.original_dir == repo.resolve()
        wm.cleanup()

    def test_existing_path_is_path_exists(self, tmp_path):
        _, root, wm = _manager(tmp_path)
        (root.root).mkdir()
        (root.root / "taken").mkdir()
        result = wm.create_result(branch_name="taken")
        assert not result.ok
        assert result.error == "path_exists"
        assert result.info is None and wm.info is None
        assert f"Worktree path already exists: {root.root / 'taken'}." in result.message
        assert f"git worktree remove {root.root / 'taken'}" in result.message

    def test_existing_file_path_is_path_exists(self, tmp_path):
        _, root, wm = _manager(tmp_path)
        root.ensure_exists()
        (root.root / "afile").write_text("x")
        assert wm.create_result(branch_name="afile").error == "path_exists"

    def test_empty_branch_name_hits_the_root_itself(self, tmp_path):
        _, _, wm = _manager(tmp_path)
        assert wm.create_result(branch_name="").error == "path_exists"

    def test_existing_branch_is_git_add_failed_with_stderr(self, tmp_path):
        repo, root, wm = _manager(tmp_path)
        subprocess.run(["git", "branch", "taken-branch"], cwd=repo, capture_output=True)
        result = wm.create_result(branch_name="taken-branch")
        assert not result.ok
        assert result.error == "git_add_failed"
        assert result.message.startswith("Failed to create worktree: ")
        assert "taken-branch" in result.message
        assert wm.info is None
        with pytest.raises(RuntimeError, match="Failed to create worktree"):
            wm.create(branch_name="taken-branch")

    def test_auto_name_uses_an_eight_char_random_suffix(self, tmp_path):
        _, _, wm = _manager(tmp_path)
        a = wm.create().branch_name
        wm.cleanup()
        b = wm.create().branch_name
        wm.cleanup()
        prefix_a, suffix_a = a.rsplit("-", 1)
        prefix_b, suffix_b = b.rsplit("-", 1)
        assert prefix_a == prefix_b
        assert len(suffix_a) == len(suffix_b) == 8
        assert suffix_a != suffix_b

    def test_empty_session_id_falls_back_to_a_random_suffix(self, tmp_path):
        _, _, wm = _manager(tmp_path)
        name = wm.create(session_id="").branch_name
        assert len(name.rsplit("-", 1)[1]) == 8
        wm.cleanup()

    def test_session_id_is_the_suffix_under_the_root(self, tmp_path):
        _, root, wm = _manager(tmp_path)
        info = wm.create(session_id="sess-9")
        assert info.branch_name.endswith("-sess-9")
        assert info.worktree_path == root.root / info.branch_name
        wm.cleanup()

    def test_unicode_and_space_free_slash_branch_nests_under_the_root(self, tmp_path):
        _, root, wm = _manager(tmp_path)
        info = wm.create(branch_name="feat/é-1")
        assert info.worktree_path == root.root / "feat" / "é-1"
        assert info.worktree_path.is_dir()
        assert wm.cleanup().ok


class TestCleanupResult:
    def test_without_a_worktree_it_is_a_noop(self, tmp_path):
        _, _, wm = _manager(tmp_path)
        result = wm.cleanup()
        assert result.status == "noop"
        assert result.ok is False
        assert result.info is None and result.stderr == ""
        assert wm.has_changes() is False

    def test_cleaned_removes_directory_and_branch_then_second_call_is_noop(self, tmp_path):
        repo, _, wm = _manager(tmp_path)
        info = wm.create(branch_name="reap")
        result = wm.cleanup()
        assert result.status == "cleaned" and result.ok
        assert not info.worktree_path.exists()
        branches = subprocess.run(
            ["git", "branch"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert "reap" not in branches
        assert wm.info is None
        assert wm.cleanup().status == "noop"

    def test_dirty_result_carries_info_and_is_not_ok(self, tmp_path):
        _, _, wm = _manager(tmp_path)
        info = wm.create(branch_name="dirty")
        (info.worktree_path / "u.txt").write_text("x")
        result = wm.cleanup()
        assert result.status == "preserved_dirty" and not result.ok
        assert result.info == info
        assert wm.info == info

    def test_staged_change_counts_as_changes(self, tmp_path):
        _, _, wm = _manager(tmp_path)
        info = wm.create(branch_name="staged")
        (info.worktree_path / "README.md").write_text("changed")
        subprocess.run(["git", "add", "README.md"], cwd=info.worktree_path, capture_output=True)
        assert wm.has_changes()

    def test_a_vanished_directory_is_still_cleaned(self, tmp_path):
        import shutil

        repo, _, wm = _manager(tmp_path)
        info = wm.create(branch_name="vanish")
        shutil.rmtree(info.worktree_path)
        assert wm.has_changes() is False
        result = wm.cleanup()
        assert result.status == "cleaned"
        assert wm.info is None
        branches = subprocess.run(
            ["git", "branch"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert "vanish" not in branches

    def test_a_locked_worktree_is_git_remove_failed(self, tmp_path):
        repo, _, wm = _manager(tmp_path)
        info = wm.create(branch_name="locked")
        subprocess.run(
            ["git", "worktree", "lock", str(info.worktree_path)], cwd=repo, capture_output=True
        )
        result = wm.cleanup()
        assert result.status == "git_remove_failed"
        assert not result.ok
        assert "locked" in result.stderr
        assert result.info == info
        assert wm.info == info
        assert info.worktree_path.exists()
        subprocess.run(
            ["git", "worktree", "unlock", str(info.worktree_path)], cwd=repo, capture_output=True
        )
        assert wm.cleanup().ok

    def test_unmerged_commits_are_branch_delete_failed(self, tmp_path):
        repo, _, wm = _manager(tmp_path)
        info = wm.create(branch_name="unmerged")
        # Commit in the worktree, then force the tree clean again.
        (info.worktree_path / "n.txt").write_text("x")
        subprocess.run(["git", "add", "n.txt"], cwd=info.worktree_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "n"], cwd=info.worktree_path, capture_output=True)
        result = wm.cleanup()
        assert result.status == "branch_delete_failed"
        assert not result.ok
        assert result.info is None
        assert "not fully merged" in result.stderr
        assert wm.info is None
        assert not info.worktree_path.exists()
        branches = subprocess.run(
            ["git", "branch"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert "unmerged" in branches


class TestReportCleanup:
    def _collect(self, wm):
        out = []
        wm.report_cleanup(lambda msg, **kw: out.append((msg, kw)))
        return out

    def test_silent_without_a_worktree(self, tmp_path):
        _, _, wm = _manager(tmp_path)
        assert self._collect(wm) == []

    def test_cleaned_message(self, tmp_path):
        _, _, wm = _manager(tmp_path)
        wm.create(branch_name="rc")
        assert self._collect(wm) == [("Worktree cleaned up (no changes).", {})]

    def test_preserved_message_names_path_branch_and_hints(self, tmp_path):
        _, _, wm = _manager(tmp_path)
        info = wm.create(branch_name="rp")
        (info.worktree_path / "x").write_text("x")
        out = self._collect(wm)
        assert [m for m, _ in out] == [
            "\nWorktree preserved (has changes):",
            f"  Path:   {info.worktree_path}",
            "  Branch: rp",
            "\nTo merge: git merge rp",
            f"To remove: git worktree remove {info.worktree_path}",
        ]
        assert all(kw == {} for _, kw in out)

    def test_failure_goes_to_stderr_with_path_and_branch(self, tmp_path):
        _, _, wm = _manager(tmp_path)
        info = wm.create(branch_name="rf")
        (info.worktree_path / "n.txt").write_text("x")
        subprocess.run(["git", "add", "n.txt"], cwd=info.worktree_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "n"], cwd=info.worktree_path, capture_output=True)
        out = self._collect(wm)
        first, kw = out[0]
        assert first.startswith("\nWorktree cleanup failed (branch_delete_failed): ")
        assert "not fully merged" in first
        assert kw == {"err": True}
        assert out[1:] == [
            (f"  Path:   {info.worktree_path}", {}),
            ("  Branch: rf", {}),
        ]
