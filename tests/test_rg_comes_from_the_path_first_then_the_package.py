"""Where ``rg`` comes from, and what happens without one.

The PyPI ``ripgrep`` package has wheels only for macOS arm64 and Linux
x86_64; its sdist needs Rust, so requiring it everywhere made
``pip install igni-core`` fail on linux/arm64. It is now required only on
those two platforms, an ``rg`` on ``PATH`` always wins, and the package's
binary — in the interpreter's scripts directory, which a ``uv tool`` install
or the desktop app's venv does not put on ``PATH`` — is the fallback for the
composer's code search and for the agent's shell.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tomllib
from pathlib import Path

import pytest

from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.tools.shell import IgniShellTools
from igni_core.core.utils import ripgrep

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX executables and symlinks")


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho fake rg\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def scripts(tmp_path, monkeypatch):
    """The interpreter's scripts directory, empty: drop an ``rg`` in it to
    stand for the installed PyPI package."""
    directory = tmp_path / "venv-bin"
    directory.mkdir()
    real = ripgrep.sysconfig.get_path
    monkeypatch.setattr(
        ripgrep.sysconfig,
        "get_path",
        lambda name, *a, **k: str(directory) if name == "scripts" else real(name, *a, **k),
    )
    return directory


@pytest.fixture
def home(tmp_path, monkeypatch):
    directory = tmp_path / "igni-home"
    monkeypatch.setattr(ripgrep, "home_config_dir", lambda: directory)
    ripgrep._link_dir.cache_clear()
    yield directory
    ripgrep._link_dir.cache_clear()


@pytest.fixture
def no_rg_path(tmp_path):
    """A PATH with nothing on it, so no ``rg`` whatever the host has
    installed (the shell itself is ``/bin/sh``, found by its path)."""
    directory = tmp_path / "path-bin"
    directory.mkdir()
    return str(directory)


# ── find_rg ─────────────────────────────────────────────────────────


def test_an_rg_on_the_path_wins_over_the_package(tmp_path, scripts):
    on_path = _executable(tmp_path / "path-bin" / "rg")
    _executable(scripts / "rg")

    assert ripgrep.find_rg(str(on_path.parent)) == str(on_path)


def test_the_package_binary_answers_when_the_path_has_none(tmp_path, scripts):
    bundled = _executable(scripts / "rg")

    assert ripgrep.find_rg(str(tmp_path / "empty")) == str(bundled)


def test_no_rg_anywhere_is_none(tmp_path, scripts):
    assert ripgrep.find_rg(str(tmp_path / "empty")) is None


def test_a_package_file_that_cannot_run_is_not_an_rg(tmp_path, scripts):
    (scripts / "rg").write_text("not executable")
    (scripts / "rg").chmod(0o644)

    assert ripgrep.bundled_rg() is None
    assert ripgrep.find_rg(str(tmp_path / "empty")) is None


# ── shell_path ──────────────────────────────────────────────────────


def test_the_shell_path_is_untouched_when_it_has_rg(tmp_path, scripts, home):
    on_path = _executable(tmp_path / "path-bin" / "rg")
    _executable(scripts / "rg")

    assert ripgrep.shell_path(str(on_path.parent)) == str(on_path.parent)
    assert not home.exists()


def test_the_shell_path_is_untouched_without_a_package_binary(scripts, home, no_rg_path):
    assert ripgrep.shell_path(no_rg_path) == no_rg_path
    assert not home.exists()


def test_the_package_binary_is_appended_alone(scripts, home, no_rg_path):
    """Appended, so the user's tools stay first; alone, so the venv's
    ``python`` and ``pip`` do not reach the agent's shell."""
    bundled = _executable(scripts / "rg")
    _executable(scripts / "python")

    path = ripgrep.shell_path(no_rg_path)

    link_dir = home / "bin"
    assert path == f"{no_rg_path}{os.pathsep}{link_dir}"
    assert sorted(p.name for p in link_dir.iterdir()) == ["rg"]
    assert os.readlink(link_dir / "rg") == str(bundled)


def test_a_stale_link_is_replaced(scripts, home, no_rg_path):
    bundled = _executable(scripts / "rg")
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "rg").symlink_to(home / "an-old-venv" / "rg")

    ripgrep.shell_path(no_rg_path)

    assert os.readlink(home / "bin" / "rg") == str(bundled)


def test_a_home_that_cannot_hold_the_link_leaves_the_path_alone(scripts, home, no_rg_path):
    _executable(scripts / "rg")
    home.parent.mkdir(parents=True, exist_ok=True)
    home.write_text("a file where the directory would go")

    assert ripgrep.shell_path(no_rg_path) == no_rg_path


def test_the_agent_shell_runs_the_package_rg(tmp_path, scripts, home, no_rg_path, monkeypatch):
    """The shell tool spawns with :func:`shell_path`, so ``rg`` typed by the
    agent runs the package's binary when ``PATH`` has none."""
    _executable(scripts / "rg")
    monkeypatch.setenv("PATH", no_rg_path)
    supervisor = ProcessSupervisor()
    tools = IgniShellTools(base_dir=tmp_path, supervisor=supervisor)
    try:
        output = asyncio.run(tools.run_shell_command("rg", timeout=10))
    finally:
        supervisor.registry.kill_all()

    assert "fake rg" in output


def test_without_any_rg_the_agent_shell_says_so_rather_than_raising(
    tmp_path, scripts, home, no_rg_path, monkeypatch
):
    monkeypatch.setenv("PATH", no_rg_path)
    supervisor = ProcessSupervisor()
    tools = IgniShellTools(base_dir=tmp_path, supervisor=supervisor)
    try:
        output = asyncio.run(tools.run_shell_command("rg needle", timeout=10))
    finally:
        supervisor.registry.kill_all()

    assert re.search(r"rg: (command )?not found|rg: No such file", output), output


# ── the message without one ─────────────────────────────────────────


def test_the_missing_message_says_how_to_install_ripgrep():
    for how in ("brew install ripgrep", "apt install ripgrep", "dnf install ripgrep"):
        assert how in ripgrep.MISSING


# ── packaging ───────────────────────────────────────────────────────

_WHEEL_PLATFORMS = (
    "(sys_platform == 'darwin' and platform_machine == 'arm64') or "
    "(sys_platform == 'linux' and platform_machine == 'x86_64')"
)


def test_the_ripgrep_package_is_required_only_where_it_has_a_wheel():
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    ripgrep_deps = [d for d in deps if re.match(r"ripgrep\b", d)]

    assert ripgrep_deps == [f"ripgrep~=15.1.0; {_WHEEL_PLATFORMS}"]


def test_the_lock_carries_the_same_marker():
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    core = next(p for p in lock["package"] if p["name"] == "igni-core")
    entry = next(d for d in core["dependencies"] if d["name"] == "ripgrep")

    assert "marker" in entry, entry
    for clause in ("sys_platform == 'darwin'", "platform_machine == 'arm64'"):
        assert clause in entry["marker"]
    for clause in ("sys_platform == 'linux'", "platform_machine == 'x86_64'"):
        assert clause in entry["marker"]
