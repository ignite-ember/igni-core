"""The person's login shell on a POSIX pseudo-terminal."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import pty
import signal
import struct
import subprocess
import termios
from pathlib import Path

from igni_core.backend.terminal.terminal_process import (
    ExitCallback,
    OutputCallback,
    TerminalProcess,
)
from igni_core.core.activity.background import BackgroundTasks

_READ_SIZE = 64 * 1024


def _take_controlling_tty() -> None:
    """In the child, after ``setsid``: make its PTY the session's terminal, so job control and Ctrl-C work."""
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class PosixPty(TerminalProcess):
    """``$SHELL -l`` (else bash, else sh) on a PTY, read through the event loop."""

    FALLBACK_SHELLS = ("/bin/bash", "/bin/sh")

    def __init__(self, *, cwd: Path, cols: int, rows: int, shell_path: str | None = None) -> None:
        self._cwd = cwd
        self._cols = cols
        self._rows = rows
        self._shell = shell_path or self.login_shell()
        self._proc: subprocess.Popen[bytes] | None = None
        self._master: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_output: OutputCallback | None = None
        self._on_exit: ExitCallback | None = None
        self._exited: asyncio.Future[int | None] | None = None
        self._reading = False
        self._paused = False
        self._input = bytearray()
        self._writing = False

    @classmethod
    def login_shell(cls) -> str:
        """The person's ``$SHELL`` when it can run, else the first fallback that can."""
        for candidate in (os.environ.get("SHELL", ""), *cls.FALLBACK_SHELLS):
            if candidate and os.path.isabs(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return cls.FALLBACK_SHELLS[-1]

    @property
    def shell(self) -> str:
        return self._shell

    @property
    def pid(self) -> int | None:
        """The shell's process id (and its process group's), once started."""
        return self._proc.pid if self._proc is not None else None

    def start(
        self, loop: asyncio.AbstractEventLoop, on_output: OutputCallback, on_exit: ExitCallback
    ) -> None:
        master, slave = pty.openpty()
        try:
            self._set_size(slave, self._cols, self._rows)
            self._proc = subprocess.Popen(  # noqa: S603 — the person's own shell, by its path
                [self._shell, "-l"],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=self._cwd,
                env=self.environment(),
                start_new_session=True,
                preexec_fn=_take_controlling_tty,  # noqa: PLW1509 — only an ioctl, after setsid
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        self._master = master
        self._loop = loop
        self._on_output = on_output
        self._on_exit = on_exit
        self._exited = loop.create_future()
        self._start_reading()
        # Not busy: an open terminal does not keep igni awake.
        BackgroundTasks.shared().thread(self._wait, name=f"terminal-{self._proc.pid}")

    def write(self, data: bytes) -> None:
        if self._master is None or not data:
            return
        self._input += data
        self._flush_input()

    def resize(self, cols: int, rows: int) -> None:
        self._cols, self._rows = cols, rows
        if self._master is None:
            return
        self._set_size(self._master, cols, rows)
        for group in self._groups():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(group, signal.SIGWINCH)

    def pause_reading(self) -> None:
        self._paused = True
        self._stop_reading()

    def resume_reading(self) -> None:
        self._paused = False
        self._start_reading()

    async def close(self) -> None:
        exited = self._exited
        if exited is None or exited.done():
            return
        self._signal(signal.SIGHUP)
        if await self._exits_within(exited, self.GRACE_S):
            return
        self._signal(signal.SIGKILL)
        await self._exits_within(exited, self.GRACE_S * 3)

    # ── reading and writing ───────────────────────────────────────

    def _start_reading(self) -> None:
        if self._reading or self._paused or self._master is None or self._loop is None:
            return
        self._loop.add_reader(self._master, self._readable)
        self._reading = True

    def _stop_reading(self) -> None:
        if self._reading and self._master is not None and self._loop is not None:
            self._loop.remove_reader(self._master)
        self._reading = False

    def _readable(self) -> None:
        """One read per wake-up, so a shell that prints without pause cannot starve the loop."""
        if self._master is None:
            return
        try:
            data = os.read(self._master, _READ_SIZE)
        except BlockingIOError:
            return
        except OSError:
            # EIO: every copy of the PTY's other end is closed.
            self._stop_reading()
            return
        if not data:
            self._stop_reading()
            return
        if self._on_output is not None:
            self._on_output(data)

    def _drain(self) -> None:
        """Read what the shell left in the PTY before it exited."""
        while self._master is not None:
            try:
                data = os.read(self._master, _READ_SIZE)
            except OSError:
                return
            if not data:
                return
            if self._on_output is not None:
                self._on_output(data)

    def _flush_input(self) -> None:
        while self._input and self._master is not None:
            try:
                written = os.write(self._master, self._input)
            except BlockingIOError:
                break
            except OSError:
                self._input.clear()
                break
            del self._input[:written]
        if self._loop is None or self._master is None:
            return
        if self._input and not self._writing:
            self._loop.add_writer(self._master, self._flush_input)
            self._writing = True
        elif not self._input and self._writing:
            self._loop.remove_writer(self._master)
            self._writing = False

    # ── the end ───────────────────────────────────────────────────

    def _wait(self) -> None:
        """On its own thread: reap the shell, then tell the loop."""
        proc = self._proc
        loop = self._loop
        if proc is None or loop is None:
            return
        code = proc.wait()
        with contextlib.suppress(RuntimeError):  # the loop closed first
            loop.call_soon_threadsafe(self._reaped, code)

    def _reaped(self, code: int | None) -> None:
        self._drain()
        self._stop_reading()
        master = self._master
        if master is not None and self._loop is not None:
            if self._writing:
                self._loop.remove_writer(master)
                self._writing = False
            self._master = None
            os.close(master)
        if self._exited is not None and not self._exited.done():
            self._exited.set_result(code)
        if self._on_exit is not None:
            on_exit, self._on_exit = self._on_exit, None
            on_exit(code)

    def _groups(self) -> set[int]:
        """The shell's process group and the one in the terminal's foreground."""
        groups: set[int] = set()
        if self._proc is not None:
            groups.add(self._proc.pid)
        if self._master is not None:
            with contextlib.suppress(OSError):
                groups.add(os.tcgetpgrp(self._master))
        return {group for group in groups if group > 0}

    def _signal(self, signum: signal.Signals) -> None:
        for group in self._groups():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(group, signum)

    @staticmethod
    async def _exits_within(exited: asyncio.Future[int | None], seconds: float) -> bool:
        try:
            await asyncio.wait_for(asyncio.shield(exited), seconds)
        except TimeoutError:
            return False
        return True

    @staticmethod
    def _set_size(fd: int, cols: int, rows: int) -> None:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


__all__ = ["PosixPty"]
