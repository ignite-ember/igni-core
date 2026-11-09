"""Which folder this is: its canonical path and its git remote.

A trust decision is recorded against both, so a moved clone or a different
repository at a trusted path asks again. The remote is read from
``.git/config`` directly, never by running ``git``: the folder is not
trusted yet, and a repository's own config can make ``git`` run commands.

This file covers the corners of that reading — the spellings of a remote
URL, a worktree whose ``.git`` is a file, a config that cannot be read —
which are the parts that decide whether a folder is asked about at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.workspace_trust import canonical_remote
from igni_core.core.workspace_trust.identity import GitConfigRemote, WorkspaceIdentity

pytestmark = pytest.mark.workspace_trust


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _config_with(url: str) -> str:
    return f'[remote "origin"]\n\turl = {url}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'


# ── the spellings of a remote ─────────────────────────────────────


def test_a_remote_without_a_scheme_or_a_host_is_nothing():
    assert canonical_remote("") == ""
    assert canonical_remote("   ") == ""


def test_a_file_url_is_a_local_path():
    """A local path is returned as written: ``app.git`` there is a directory
    name, not the repository-name suffix a host's URL carries."""
    assert canonical_remote("file:///srv/git/app.git") == "/srv/git/app.git"
    assert canonical_remote("file:///srv/git/app/") == "/srv/git/app"


def test_a_local_path_is_left_as_it_is():
    assert canonical_remote("/srv/git/app/") == "/srv/git/app"
    assert canonical_remote("~/code/app") == "~/code/app"
    assert canonical_remote("./relative/app") == "./relative/app"


def test_a_port_is_not_part_of_the_host():
    assert canonical_remote("ssh://git@github.com:22/acme/app.git") == "github.com/acme/app"
    assert canonical_remote("https://github.com:8443/acme/app.git") == "github.com/acme/app"


# ── reading the config ────────────────────────────────────────────


def test_the_remote_of_a_plain_repository(tmp_path):
    repo = tmp_path / "repo"
    _write(repo / ".git" / "config", _config_with("git@github.com:acme/app.git"))

    assert GitConfigRemote.read(repo) == "git@github.com:acme/app.git"
    assert WorkspaceIdentity.of(repo).remote == "github.com/acme/app"


def test_a_remote_that_is_not_origin_or_upstream_is_still_the_remote(tmp_path):
    repo = tmp_path / "repo"
    _write(repo / ".git" / "config", '[remote "fork"]\n\turl = git@github.com:acme/app.git\n')

    assert GitConfigRemote.read(repo) == "git@github.com:acme/app.git"


def test_a_config_that_cannot_be_read_has_no_remote(tmp_path):
    """A ``config`` that is a directory cannot be read; the folder asks."""
    repo = tmp_path / "repo"
    (repo / ".git" / "config").mkdir(parents=True)

    assert GitConfigRemote.read(repo) is None


def test_a_worktree_reads_the_config_of_its_git_dir(tmp_path):
    """A worktree's ``.git`` is a file naming the git dir it lives in."""
    repo = tmp_path / "repo"
    git_dir = tmp_path / "main" / ".git" / "worktrees" / "wt"
    _write(git_dir / "config", _config_with("git@github.com:acme/app.git"))
    _write(repo / ".git", f"gitdir: {git_dir}\n")

    assert GitConfigRemote.read(repo) == "git@github.com:acme/app.git"


def test_a_worktree_that_names_its_git_dir_relatively(tmp_path):
    """The pointer may be relative; it is read against the folder it is in."""
    repo = tmp_path / "repo"
    git_dir = tmp_path / "main" / ".git" / "worktrees" / "wt"
    _write(git_dir / "config", _config_with("git@github.com:acme/app.git"))
    _write(repo / ".git", "gitdir: ../main/.git/worktrees/wt\n")

    assert GitConfigRemote.read(repo) == "git@github.com:acme/app.git"


def test_a_worktree_that_names_a_shared_git_dir_reads_that_one(tmp_path):
    """``commondir`` says where the objects live; the config is there."""
    repo = tmp_path / "repo"
    shared = tmp_path / "main" / ".git"
    git_dir = shared / "worktrees" / "wt"
    _write(shared / "config", _config_with("git@github.com:acme/app.git"))
    _write(git_dir / "commondir", "../..\n")
    _write(repo / ".git", f"gitdir: {git_dir}\n")

    assert GitConfigRemote.read(repo) == "git@github.com:acme/app.git"


def test_a_dot_git_file_that_names_no_git_dir_has_no_remote(tmp_path):
    repo = tmp_path / "repo"
    _write(repo / ".git", "this is not a gitdir pointer\n")

    assert GitConfigRemote.read(repo) is None


def test_an_empty_dot_git_file_has_no_remote(tmp_path):
    repo = tmp_path / "repo"
    _write(repo / ".git", "")

    assert GitConfigRemote.read(repo) is None


def test_a_folder_outside_a_repository_has_no_remote(tmp_path):
    assert GitConfigRemote.read(tmp_path) is None
