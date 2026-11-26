"""The shell a terminal runs, and the PTY it runs on, are this machine's.

POSIX: ``$SHELL`` when it can run, else bash, else sh, always ``-l``.
Windows: ``%COMSPEC%`` else ``powershell.exe`` on ConPTY through
``pywinpty``; without it the terminal is unavailable. The Windows half is
driven against a stand-in ``winpty`` module.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import threading
import types

import pytest

from igni_core.backend.terminal.exceptions import TerminalUnavailable
from igni_core.backend.terminal.posix_pty import PosixPty
from igni_core.backend.terminal.terminal_process import TerminalProcess
from igni_core.backend.terminal.windows_pty import WindowsPty
from tests.terminal_helpers import wait_until

# ── POSIX ──────────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shells")
def test_the_person_s_shell_wins_when_it_can_run(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    assert PosixPty.login_shell() == "/bin/sh"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shells")
@pytest.mark.parametrize("shell", ["", "zsh", "/no/such/shell"])
def test_a_shell_that_cannot_run_falls_back_to_bash(monkeypatch, shell):
    monkeypatch.setenv("SHELL", shell)
    assert PosixPty.login_shell() == "/bin/bash"


def test_with_neither_bash_nor_a_shell_it_is_sh(monkeypatch):
    monkeypatch.delenv("SHELL", raising=False)
    monkeypatch.setattr(PosixPty, "FALLBACK_SHELLS", ("/no/bash", "/bin/sh"))
    assert PosixPty.login_shell() == "/bin/sh"
    monkeypatch.setattr(PosixPty, "FALLBACK_SHELLS", ("/no/bash", "/no/sh"))
    assert PosixPty.login_shell() == "/no/sh"


def test_the_environment_is_igni_s_as_a_colour_terminal(monkeypatch):
    monkeypatch.setenv("IGNI_TERMINAL_PROBE", "kept")
    monkeypatch.setenv("TERM", "dumb")
    env = TerminalProcess.environment()
    assert env["TERM"] == "xterm-256color"
    assert env["COLORTERM"] == "truecolor"
    assert env["IGNI_TERMINAL_PROBE"] == "kept"
    assert os.environ["TERM"] == "dumb"


def test_this_machine_s_pty_is_chosen(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "name", "posix")
    posix = TerminalProcess.for_this_platform(tmp_path, 80, 24)
    assert isinstance(posix, PosixPty)
    monkeypatch.setattr(os, "name", "nt")
    windows = TerminalProcess.for_this_platform(tmp_path, 80, 24)
    assert isinstance(windows, WindowsPty)


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX PTY")
async def test_an_unstarted_pty_has_nothing_to_close(tmp_path):
    pty = PosixPty(cwd=tmp_path, cols=80, rows=24, shell_path="/bin/sh")
    assert pty.pid is None
    pty.write(b"ignored")
    pty.resize(10, 10)
    await pty.close()


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX PTY")
async def test_a_large_paste_reaches_the_shell_whole(tmp_path, monkeypatch):
    """More than the PTY takes at once: the rest waits for it to drain."""
    monkeypatch.setenv("HOME", str(tmp_path))
    out = bytearray()
    exited: list[int | None] = []
    pty = PosixPty(cwd=tmp_path, cols=80, rows=24, shell_path="/bin/sh")
    pty.start(asyncio.get_running_loop(), out.extend, exited.append)
    try:
        pty.write(b"stty -echo -icanon; head -c 200000 | wc -c; exit\n")
        await asyncio.sleep(0.3)
        pty.write(b"x" * 200_000)
        await wait_until(lambda: bool(exited), 20)
    finally:
        await pty.close()
    assert b"200000" in bytes(out)


# ── Windows, against a stand-in winpty ─────────────────────────────


class _FakeWinPty:
    """``winpty.PtyProcess`` as far as WindowsPty uses it."""

    spawned: list[_FakeWinPty] = []

    def __init__(self, argv, cwd, env, dimensions):
        self.argv, self.cwd, self.env, self.dimensions = argv, cwd, env, dimensions
        self.reads: list[str] = ["hé", "llo"]
        self.written: list[str] = []
        self.sizes: list[tuple[int, int]] = []
        self.exitstatus: int | None = None
        self.terminated = threading.Event()
        self.closed = False

    @classmethod
    def spawn(cls, argv, cwd=None, env=None, dimensions=(24, 80)):
        proc = cls(argv, cwd, env, dimensions)
        cls.spawned.append(proc)
        return proc

    def read(self, size):
        if self.reads:
            return self.reads.pop(0)
        self.terminated.wait(5)
        raise EOFError

    def write(self, text):
        self.written.append(text)

    def setwinsize(self, rows, cols):
        self.sizes.append((rows, cols))

    def terminate(self, force=False):
        self.exitstatus = 1
        self.terminated.set()

    def wait(self):
        return self.exitstatus

    def close(self):
        self.closed = True


@pytest.fixture
def fake_winpty(monkeypatch):
    _FakeWinPty.spawned = []
    module = types.SimpleNamespace(PtyProcess=_FakeWinPty)
    real = importlib.import_module

    def import_module(name, *args):
        return module if name == "winpty" else real(name, *args)

    monkeypatch.setattr(importlib, "import_module", import_module)
    return module


async def test_windows_runs_comspec_on_a_conpty_and_reads_it_off_the_loop(
    fake_winpty, monkeypatch, tmp_path
):
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    out = bytearray()
    exited: list[int | None] = []
    pty = WindowsPty(cwd=tmp_path, cols=100, rows=30)
    assert pty.shell == r"C:\Windows\System32\cmd.exe"

    pty.start(asyncio.get_running_loop(), out.extend, exited.append)
    (proc,) = _FakeWinPty.spawned
    assert proc.argv == [r"C:\Windows\System32\cmd.exe"]
    assert proc.cwd == str(tmp_path)
    assert proc.dimensions == (30, 100)
    assert proc.env["TERM"] == "xterm-256color"
    await wait_until(lambda: bytes(out) == "héllo".encode())

    pty.write(b"dir\r")
    pty.resize(120, 40)
    assert proc.written == ["dir\r"]
    assert proc.sizes == [(40, 120)]

    await pty.close()
    assert exited == [1]
    assert proc.closed
    await pty.close()
    assert exited == [1]


def test_windows_falls_back_to_powershell(monkeypatch, tmp_path):
    monkeypatch.delenv("COMSPEC", raising=False)
    assert WindowsPty(cwd=tmp_path, cols=1, rows=1).shell == "powershell.exe"


async def test_windows_waits_while_reading_is_paused(fake_winpty, tmp_path):
    out = bytearray()
    pty = WindowsPty(cwd=tmp_path, cols=80, rows=24)
    pty.pause_reading()
    pty.start(asyncio.get_running_loop(), out.extend, lambda code: None)
    await asyncio.sleep(0.1)
    assert out == b""
    pty.resume_reading()
    await wait_until(lambda: bytes(out) == "héllo".encode())
    await pty.close()


async def test_windows_without_pywinpty_is_unavailable(monkeypatch, tmp_path):
    real = importlib.import_module

    def import_module(name, *args):
        if name == "winpty":
            raise ImportError("No module named 'winpty'")
        return real(name, *args)

    monkeypatch.setattr(importlib, "import_module", import_module)
    pty = WindowsPty(cwd=tmp_path, cols=80, rows=24)

    with pytest.raises(TerminalUnavailable, match="pywinpty"):
        pty.start(asyncio.get_running_loop(), lambda data: None, lambda code: None)
    await pty.close()


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX PTY")
async def test_a_paused_pty_is_not_read_until_it_resumes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    out = bytearray()
    pty = PosixPty(cwd=tmp_path, cols=80, rows=24, shell_path="/bin/sh")
    pty.start(asyncio.get_running_loop(), out.extend, lambda code: None)
    try:
        pty.pause_reading()
        pty.write(b"printf 'PA%sED\\n' US\n")
        await asyncio.sleep(0.5)
        assert b"PAUSED" not in bytes(out)
        pty.resume_reading()
        await wait_until(lambda: b"PAUSED" in bytes(out))
    finally:
        await pty.close()


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX PTY")
async def test_a_shell_that_ignores_the_hang_up_is_killed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(PosixPty, "GRACE_S", 0.3)
    out = bytearray()
    exited: list[int | None] = []
    pty = PosixPty(cwd=tmp_path, cols=80, rows=24, shell_path="/bin/sh")
    pty.start(asyncio.get_running_loop(), out.extend, exited.append)
    pty.write(b"trap '' HUP; printf 'TR%sED\\n' APP\n")
    await wait_until(lambda: b"TRAPPED" in bytes(out))

    await pty.close()

    assert exited == [-9]
    assert pty.pid is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(pty.pid, 0)
