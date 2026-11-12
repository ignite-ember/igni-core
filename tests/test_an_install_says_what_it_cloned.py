"""``igni ext install`` from a repository says what it cloned and at which commit.

"Latest" is a legitimate thing to install, and the commit it meant today is
the fact worth printing. A directory is not cloned, so it says neither.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from igni_core.cli.ext import ext


def _repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "mirror"
    root.mkdir()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "id": "acme.notes",
                "version": "0.0.1",
                "contract": ">=1.0 <2",
                "trust": "unsigned",
                "dev": {"command": ["true"]},
            }
        )
    )
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run([*git, "add", "manifest.json"], check=True)
    subprocess.run([*git, "commit", "-qm", "v1"], check=True)
    sha = subprocess.run(
        [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    return root, sha


@pytest.fixture(autouse=True)
def _dev_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IGNI_DEV_EXTENSIONS", "1")


def _install(source: str, tmp_path: Path, *args: str):
    return CliRunner().invoke(ext, ["install", source, "--root", str(tmp_path / "s"), *args])


def test_a_repository_is_announced_before_the_clone_and_its_commit_after(tmp_path):
    root, sha = _repo(tmp_path)
    result = _install(f"file://{root}", tmp_path)
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == f"cloning file://{root} …"
    assert lines[1] == f"  at {sha}"
    assert lines[2].startswith("installed acme.notes")
    assert re.fullmatch(r"  at [0-9a-f]{40}", lines[1])


def test_a_ref_is_named_in_the_announcement(tmp_path):
    root, sha = _repo(tmp_path)
    result = _install(f"file://{root}", tmp_path, "--ref", "main")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == f"cloning file://{root} at main …"
    assert f"  at {sha}" in result.output


def test_a_directory_is_not_cloned_so_nothing_is_announced(tmp_path):
    root, _ = _repo(tmp_path)
    result = _install(str(root), tmp_path)
    assert result.exit_code == 0, result.output
    assert "cloning" not in result.output
    assert result.output.startswith("installed acme.notes")


def test_a_repository_that_cannot_be_cloned_is_a_message(tmp_path):
    result = _install(f"file://{tmp_path / 'nowhere'}", tmp_path)
    assert result.exit_code != 0
    assert "cloning" in result.output and "failed" in result.output
    assert "Traceback" not in result.output
    assert not (tmp_path / "s" / "acme.notes").exists()
