"""An update moves a plugin to what the remote has now.

* ``update`` to a branch reset to the clone's own copy of that branch —
  the commit it was installed at — so "Updated to <sha>" named the old
  commit and the plugin did not move.
* A plugin installed at a tag was cloned single-branch, so updating it
  without a ref died with a raw ``git`` error about ``origin/main``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from igni_core.core.plugins.installer import PluginInstaller
from igni_core.core.plugins.state import load_state


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


@pytest.fixture
def remote(tmp_path) -> Path:
    root = tmp_path / "remote"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "demo"}))
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "one")
    _git(root, "tag", "v1")
    return root


def _commit_again(remote: Path) -> str:
    (remote / "more.txt").write_text("more")
    _git(remote, "add", "-A")
    _git(remote, "commit", "-qm", "two")
    return _git(remote, "rev-parse", "HEAD")


def _installer(tmp_path: Path) -> PluginInstaller:
    return PluginInstaller(data_dir=tmp_path / "data")


def test_updating_to_a_branch_takes_the_remote_s_newest_commit(tmp_path, remote):
    installer = _installer(tmp_path)
    installer.install(str(remote))
    newest = _commit_again(remote)

    moved = installer.update("demo", ref="main")

    assert moved == newest
    assert load_state(tmp_path / "data").pins["demo"] == newest


def test_a_plugin_installed_at_a_tag_updates_to_the_default_branch(tmp_path, remote):
    installer = _installer(tmp_path)
    installer.install(str(remote), ref="v1")
    newest = _commit_again(remote)

    assert installer.update("demo") == newest


def test_a_plugin_installed_at_a_tag_updates_to_a_branch_and_back_to_the_tag(tmp_path, remote):
    installer = _installer(tmp_path)
    installer.install(str(remote), ref="v1")
    first = _git(remote, "rev-parse", "v1")
    newest = _commit_again(remote)

    assert installer.update("demo", ref="main") == newest
    assert installer.update("demo", ref="v1") == first
