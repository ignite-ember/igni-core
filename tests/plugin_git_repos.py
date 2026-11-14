"""Real local git repositories holding a plugin, for the plugin installer's tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` with a fixed identity; return its stdout."""
    done = subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def plugin_repo(root: Path, name: str = "demo", *, manifest_name: str | None = None) -> Path:
    """A repository on ``main`` whose root is a plugin named ``manifest_name or name``."""
    repo = root / name
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": manifest_name or name}), encoding="utf-8"
    )
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "one")
    return repo


def commit_again(repo: Path) -> str:
    """Add a commit to ``repo`` and return its sha."""
    (repo / "more.txt").write_text("more", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "two")
    return git(repo, "rev-parse", "HEAD")
