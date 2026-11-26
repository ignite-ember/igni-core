"""A shell running on a pseudo-terminal, whatever the platform."""

from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

#: Called with bytes the shell printed, on the event loop.
OutputCallback = Callable[[bytes], None]
#: Called once with the shell's exit code (``None`` when unknown), on the
#: event loop, after the last output.
ExitCallback = Callable[[int | None], None]


class TerminalProcess(ABC):
    """One shell on a PTY: started, typed into, resized and ended."""

    #: How long an ended shell has to go after its hang-up before it is killed.
    GRACE_S = 1.0

    @property
    @abstractmethod
    def shell(self) -> str:
        """The shell this runs."""

    @abstractmethod
    def start(
        self, loop: asyncio.AbstractEventLoop, on_output: OutputCallback, on_exit: ExitCallback
    ) -> None:
        """Start the shell; raises ``TerminalUnavailable`` or ``OSError`` when it cannot."""

    @abstractmethod
    def write(self, data: bytes) -> None:
        """Type ``data`` into the shell."""

    @abstractmethod
    def resize(self, cols: int, rows: int) -> None:
        """Tell the PTY and the shell its new size."""

    @abstractmethod
    def pause_reading(self) -> None:
        """Stop reading output until :meth:`resume_reading`; the shell blocks when the PTY fills."""

    @abstractmethod
    def resume_reading(self) -> None:
        """Read output again."""

    @abstractmethod
    async def close(self) -> None:
        """End the shell and everything it started; returns once it has exited."""

    @staticmethod
    def environment() -> dict[str, str]:
        """The shell's environment: igni's own, as a colour terminal."""
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        return env

    @staticmethod
    def for_this_platform(cwd: Path, cols: int, rows: int) -> TerminalProcess:
        """The PTY this machine has: ConPTY through ``pywinpty`` on Windows, a POSIX PTY elsewhere."""
        if os.name == "nt":
            from igni_core.backend.terminal.windows_pty import WindowsPty

            return WindowsPty(cwd=cwd, cols=cols, rows=rows)
        from igni_core.backend.terminal.posix_pty import PosixPty

        return PosixPty(cwd=cwd, cols=cols, rows=rows)


__all__ = ["ExitCallback", "OutputCallback", "TerminalProcess"]
