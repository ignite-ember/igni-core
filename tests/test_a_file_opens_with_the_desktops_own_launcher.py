"""``FileOpener`` hands a path to the desktop and says whether it took it.

The launchers are real programs here: a script named ``open`` or
``xdg-open`` at the front of ``PATH`` that records what it was given and
exits as told. That is what separates "the launcher accepted it" from
"a call was made".
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

from igni_core.backend import file_opener
from igni_core.backend.file_opener import FileOpener

posix_only = pytest.mark.skipif(os.name == "nt", reason="the launchers are shell scripts")


def _launcher(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(exist_ok=True)
    script = directory / name
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return script


@pytest.fixture
def desk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory that is all of ``PATH``; the launchers go in it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    return bin_dir


@posix_only
@pytest.mark.parametrize(
    ("platform", "launcher"), [("darwin", "open"), ("linux", "xdg-open"), ("freebsd14", "xdg-open")]
)
def test_each_platform_asks_its_own_launcher(desk, monkeypatch, tmp_path, platform, launcher):
    seen = tmp_path / "seen"
    _launcher(desk, launcher, f'printf "%s" "$1" > "{seen}"')
    monkeypatch.setattr(sys, "platform", platform)

    assert FileOpener.open(Path("/some/where/report.pdf")) is True
    assert seen.read_text() == "/some/where/report.pdf"


@posix_only
def test_a_path_with_spaces_a_leading_dash_or_unicode_is_one_argument(desk, monkeypatch, tmp_path):
    seen = tmp_path / "seen"
    _launcher(desk, "xdg-open", f'printf "%s|" "$#" "$1" > "{seen}"')
    monkeypatch.setattr(sys, "platform", "linux")

    assert FileOpener.open(Path("-weird name; touch pwned/ünï.txt")) is True
    assert seen.read_text() == "1|-weird name; touch pwned/ünï.txt|"
    assert not Path("pwned").exists()


@posix_only
def test_a_launcher_that_exits_non_zero_is_a_failure_and_its_stderr_is_logged(
    desk, monkeypatch, caplog
):
    """The stderr is not UTF-8 and must not turn the failure into a crash."""
    _launcher(desk, "xdg-open", "printf '\\377\\376 nothing handles it' >&2; exit 3")
    monkeypatch.setattr(sys, "platform", "linux")

    with caplog.at_level(logging.INFO, logger=file_opener.logger.name):
        assert FileOpener.open(Path("/x/y.bin")) is False

    assert "exited 3" in caplog.text
    assert "nothing handles it" in caplog.text


@posix_only
def test_a_missing_launcher_is_a_failure_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(sys, "platform", "linux")

    assert FileOpener.open(Path("/x/y.txt")) is False


@posix_only
def test_a_launcher_that_hangs_is_a_failure_after_the_timeout(desk, monkeypatch):
    _launcher(desk, "xdg-open", "exec sleep 30")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(file_opener, "LAUNCH_TIMEOUT_SECONDS", 0.3)

    assert FileOpener.open(Path("/x/y.txt")) is False


def test_windows_hands_the_path_to_startfile(monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "startfile", started.append, raising=False)

    assert FileOpener.open(Path("C:/docs/a b.docx")) is True
    assert started == [str(Path("C:/docs/a b.docx"))]


def test_windows_refusing_the_path_is_a_failure_not_an_error(monkeypatch):
    def refuse(_path: str) -> None:
        raise OSError("no application is associated")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "startfile", refuse, raising=False)

    assert FileOpener.open(Path("C:/docs/a.zzz")) is False
