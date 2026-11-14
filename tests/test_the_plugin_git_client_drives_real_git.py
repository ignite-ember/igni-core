"""``GitClient`` against real local repositories.

The mocked-subprocess tests in ``test_plugins_git.py`` pin the error mapping;
these pin what each call does to a repository: how a clone is shaped, that
``fetch_ref`` reaches a commit a single-branch clone does not have, that
``has_ref`` answers from the local repository alone, and how the origin and the
default branch are read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.plugins.git import GitClient, GitError
from tests.plugin_git_repos import commit_again, git, plugin_repo

pytestmark = pytest.mark.skipif(not GitClient().is_available(), reason="git not available")


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    repo = plugin_repo(tmp_path)
    git(repo, "tag", "v1")
    return repo


def _is_shallow(repo: Path) -> bool:
    return git(repo, "rev-parse", "--is-shallow-repository") == "true"


class TestLooksLikeSha:
    @pytest.mark.parametrize(
        ("ref", "expected"),
        [
            ("abcdef", False),
            ("abcdef0", True),
            ("a" * 40, True),
            ("a" * 41, False),
            ("ABCDEF0", True),
            ("AbCdEf0123", True),
            ("abcdefg", False),
            ("deadbeef", True),
            ("v1.0.0", False),
            ("main", False),
            ("", False),
        ],
    )
    def test_only_seven_to_forty_hex_characters_are_a_commit(self, ref, expected):
        assert GitClient.looks_like_sha(ref) is expected


class TestClone:
    def test_it_is_shallow_by_default(self, tmp_path, remote):
        commit_again(remote)
        GitClient().clone(f"file://{remote}", tmp_path / "dest")

        assert _is_shallow(tmp_path / "dest")

    def test_shallow_false_takes_the_history(self, tmp_path, remote):
        commit_again(remote)
        GitClient().clone(f"file://{remote}", tmp_path / "dest", shallow=False)

        assert not _is_shallow(tmp_path / "dest")
        assert git(tmp_path / "dest", "rev-list", "--count", "HEAD") == "2"

    def test_a_ref_picks_the_branch_or_tag(self, tmp_path, remote):
        first = git(remote, "rev-parse", "HEAD")
        commit_again(remote)
        GitClient().clone(str(remote), tmp_path / "dest", ref="v1")

        assert GitClient().current_sha(tmp_path / "dest") == first

    def test_no_ref_takes_the_default_branch(self, tmp_path, remote):
        newest = commit_again(remote)
        GitClient().clone(str(remote), tmp_path / "dest", ref=None)

        assert GitClient().current_sha(tmp_path / "dest") == newest

    def test_cloning_into_a_non_empty_directory_is_a_git_error(self, tmp_path, remote):
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "file").write_text("x")

        with pytest.raises(GitError, match="already exists and is not an empty directory"):
            GitClient().clone(str(remote), dest)

    def test_an_unknown_ref_is_git_s_own_error(self, tmp_path, remote):
        with pytest.raises(GitError, match="nope"):
            GitClient().clone(str(remote), tmp_path / "dest", ref="nope")


class TestFetchAndRefs:
    def test_fetch_ref_leaves_the_remote_s_commit_at_fetch_head(self, tmp_path, remote):
        client = GitClient()
        client.clone(str(remote), tmp_path / "dest", ref="v1")
        newest = commit_again(remote)

        assert not client.has_ref(tmp_path / "dest", "origin/main")
        client.fetch_ref(tmp_path / "dest", "main")

        assert git(tmp_path / "dest", "rev-parse", "FETCH_HEAD") == newest

    def test_fetch_ref_of_an_unknown_ref_is_a_git_error(self, tmp_path, remote):
        client = GitClient()
        client.clone(str(remote), tmp_path / "dest")

        with pytest.raises(GitError, match="nope"):
            client.fetch_ref(tmp_path / "dest", "nope")

    def test_fetch_takes_tags_a_single_branch_clone_would_not_follow(self, tmp_path, remote):
        client = GitClient()
        client.clone(f"file://{remote}", tmp_path / "dest")
        git(remote, "checkout", "-q", "-b", "side")
        side = commit_again(remote)
        git(remote, "tag", "side-release")
        git(remote, "checkout", "-q", "main")

        client.fetch(tmp_path / "dest")

        assert git(tmp_path / "dest", "rev-parse", "side-release^{commit}") == side

    def test_has_ref_is_true_for_a_commit_and_false_for_anything_else(self, tmp_path, remote):
        client = GitClient()
        client.clone(str(remote), tmp_path / "dest")
        sha = client.current_sha(tmp_path / "dest")

        assert client.has_ref(tmp_path / "dest", "HEAD")
        assert client.has_ref(tmp_path / "dest", sha)
        assert client.has_ref(tmp_path / "dest", "origin/main")
        assert not client.has_ref(tmp_path / "dest", "origin/gone")
        assert not client.has_ref(tmp_path / "dest", "0" * 40)

    def test_has_ref_contacts_nothing(self, tmp_path, remote):
        client = GitClient()
        client.clone(str(remote), tmp_path / "dest")
        commit_again(remote)

        assert git(tmp_path / "dest", "rev-parse", "origin/main") != git(
            remote, "rev-parse", "HEAD"
        )
        assert client.has_ref(tmp_path / "dest", "origin/main")

    def test_reset_hard_moves_the_working_tree_and_checkout_a_ref(self, tmp_path, remote):
        client = GitClient()
        client.clone(str(remote), tmp_path / "dest", shallow=False)
        first = client.current_sha(tmp_path / "dest")
        commit_again(remote)
        client.fetch_ref(tmp_path / "dest", "main")

        client.reset_hard(tmp_path / "dest", "FETCH_HEAD")
        assert (tmp_path / "dest" / "more.txt").is_file()

        client.checkout(tmp_path / "dest", first)
        assert client.current_sha(tmp_path / "dest") == first
        assert not (tmp_path / "dest" / "more.txt").exists()


class TestIntrospection:
    def test_remote_url_is_the_origin_the_clone_came_from(self, tmp_path, remote):
        GitClient().clone(str(remote), tmp_path / "dest")

        assert GitClient().remote_url(tmp_path / "dest") == str(remote)

    def test_remote_url_is_none_without_an_origin_or_a_repository(self, tmp_path, remote):
        assert GitClient().remote_url(remote) is None
        assert GitClient().remote_url(tmp_path / "missing") is None

    def test_remote_url_is_none_when_the_origin_is_blank(self, tmp_path, remote):
        git(remote, "config", "remote.origin.url", "")

        assert GitClient().remote_url(remote) is None

    def test_head_branch_is_the_origin_default_branch(self, tmp_path):
        remote = plugin_repo(tmp_path)
        git(remote, "branch", "-m", "trunk")
        GitClient().clone(str(remote), tmp_path / "dest")

        assert GitClient().head_branch(tmp_path / "dest") == "trunk"

    def test_head_branch_falls_back_to_main_without_an_origin_head(self, tmp_path, remote):
        assert GitClient().head_branch(remote) == "main"
