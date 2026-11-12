"""The version the package reports, and the command that starts it.

``__version__`` is the backend's wire version and what the update checker
compares, so it must be the version the wheel is built with. ``igni`` and
``ignite-ember`` both start ``igni_core.__main__:main``, which moves a
legacy home directory out of the way before the CLI reads anything.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import igni_core

ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_reported_version_is_the_version_the_wheel_is_built_with():
    assert igni_core.__version__ == _pyproject()["project"]["version"]
    assert igni_core.__version__ == "1.0.4"


def test_both_commands_start_the_same_main():
    scripts = _pyproject()["project"]["scripts"]

    assert scripts["igni"] == "igni_core.__main__:main"
    assert scripts["ignite-ember"] == "igni_core.__main__:main"


def _run(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    return subprocess.run(
        [sys.executable, "-m", "igni_core", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=home,
        timeout=120,
    )


def test_running_the_module_moves_a_legacy_home_before_the_cli_runs(tmp_path):
    legacy = tmp_path / ".ember"
    legacy.mkdir()
    (legacy / "credentials.json").write_text('{"k": 1}', encoding="utf-8")

    result = _run(tmp_path, "--help")

    assert result.returncode == 0, result.stderr
    assert "Usage" in result.stdout
    assert not legacy.exists()
    assert (tmp_path / ".igni" / "credentials.json").read_text(encoding="utf-8") == '{"k": 1}'


def test_running_the_module_twice_leaves_a_migrated_home_alone(tmp_path):
    legacy = tmp_path / ".ember"
    legacy.mkdir()
    (legacy / "client_state.db").write_text("state", encoding="utf-8")

    first = _run(tmp_path, "--help")
    second = _run(tmp_path, "--help")

    assert first.returncode == 0 and second.returncode == 0
    assert (tmp_path / ".igni" / "client_state.db").read_text(encoding="utf-8") == "state"
    assert not legacy.exists()


def test_the_cli_exit_code_is_the_commands_exit_code(tmp_path):
    result = _run(tmp_path, "no-such-command")

    assert result.returncode == 2
    assert "No such command" in result.stderr
