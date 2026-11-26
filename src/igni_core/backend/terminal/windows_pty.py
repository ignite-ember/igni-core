"""A shell on a Windows ConPTY, through ``pywinpty``."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
import threading
from pathlib import Path
from typing import Any

from igni_core.backend.terminal.exceptions import TerminalUnavailable
from igni_core.backend.terminal.terminal_process import (
    ExitCallback,
    OutputCallback,
    TerminalProcess,
)
from igni_core.core.activity.background import BackgroundTasks

_READ_SIZE = 64 * 1024


class WindowsPty(TerminalProcess):
    """``%COMSPEC%`` (else ``powershell.exe``) on a ConPTY, read on a worker thread."""

    def __init__(self, *, cwd: Path, cols: int, rows: int) -> None:
        self._cwd = cwd
        self._cols = cols
        self._rows = rows
        self._shell = os.environ.get("COMSPEC") or "powershell.exe"
        self._proc: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._exited: asyncio.Future[int | None] | None = None
        self._readable = threading.Event()
        self._readable.set()

    @property
    def shell(self) -> str:
        return self._shell

    def start(
        self, loop: asyncio.AbstractEventLoop, on_output: OutputCallback, on_exit: ExitCallback
    ) -> None:
        try:
            winpty = importlib.import_module("winpty")
        except ImportError as exc:
            raise TerminalUnavailable(
                "the terminal needs pywinpty on Windows, and it is not installed"
            ) from exc
        self._proc = winpty.PtyProcess.spawn(
            [self._shell],
            cwd=str(self._cwd),
            env=self.environment(),
            dimensions=(self._rows, self._cols),
        )
        self._loop = loop
        self._exited = loop.create_future()
        # Not busy: an open terminal does not keep igni awake.
        BackgroundTasks.shared().thread(
            lambda: self._read(loop, on_output, on_exit), name="terminal"
        )

    def write(self, data: bytes) -> None:
        if self._proc is not None and data:
            with contextlib.suppress(OSError, EOFError):
                self._proc.write(data.decode("utf-8", errors="replace"))

    def resize(self, cols: int, rows: int) -> None:
        self._cols, self._rows = cols, rows
        if self._proc is not None:
            with contextlib.suppress(OSError):
                self._proc.setwinsize(rows, cols)

    def pause_reading(self) -> None:
        self._readable.clear()

    def resume_reading(self) -> None:
        self._readable.set()

    async def close(self) -> None:
        exited = self._exited
        if exited is None or exited.done():
            return
        with contextlib.suppress(OSError, EOFError):
            self._proc.terminate(force=True)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(exited), self.GRACE_S * 3)

    def _read(
        self, loop: asyncio.AbstractEventLoop, on_output: OutputCallback, on_exit: ExitCallback
    ) -> None:
        """On its own thread: read until the shell ends, handing each read to the loop."""
        proc = self._proc
        while True:
            self._readable.wait()
            try:
                text = proc.read(_READ_SIZE)
            except (EOFError, OSError):
                break
            if text:
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(on_output, text.encode("utf-8"))
        with contextlib.suppress(Exception):
            proc.wait()
        code = getattr(proc, "exitstatus", None)
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._reaped, code, on_exit)

    def _reaped(self, code: int | None, on_exit: ExitCallback) -> None:
        with contextlib.suppress(Exception):
            self._proc.close()
        if self._exited is not None and not self._exited.done():
            self._exited.set_result(code)
        on_exit(code)


__all__ = ["WindowsPty"]
