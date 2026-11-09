"""Test doubles for the tracked-process contract.

The registry's public API says it holds
:class:`~igni_core.core.tools.process_registry_schemas.TrackedProcess`
— the surface shared by the live, orphaned and finished kinds. A test
that wants a process in the registry can no longer hand over a
``SimpleNamespace`` and hope; it owes the contract.

These doubles implement it structurally (not by subclassing the
Protocol — an explicit implementation is what makes the type checker
verify every member). They are deliberately dumb: the point is to be a
process the registry can hold, not to model a real subprocess.

Used by the watcher and sleep-blocker tests.
"""

from __future__ import annotations

import asyncio
from typing import Any

from igni_core.core.tools.output_tail import OutputTail
from igni_core.core.tools.process_registry_schemas import ProcessHandle


class FakeProc:
    """Minimal ``asyncio.subprocess.Process`` stand-in."""

    def __init__(self, pid: int = 4242, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.killed = False

    def kill(self) -> None:
        self.killed = True


class FakeTrackedProcess:
    """A process the registry can hold, read from and finish.

    Structural implementation of :class:`TrackedProcess` — every
    member the contract declares is here, so a call site that passes
    one of these is type-checked against the same surface the real
    three kinds satisfy.
    """

    def __init__(
        self,
        pid: int = 4242,
        cmd: str = "sleep 1",
        *,
        output: list[str] | None = None,
        returncode: int | None = None,
        running: bool = True,
        elapsed_seconds: float = 0.0,
        reader_task: asyncio.Task[Any] | None = None,
    ) -> None:
        self._proc = FakeProc(pid=pid, returncode=returncode)
        self._cmd = cmd
        self._output = list(output or [])
        self._running = running
        self._elapsed_seconds = elapsed_seconds
        self._reader_task = reader_task

    # ── The contract ────────────────────────────────────────────

    @property
    def proc(self) -> ProcessHandle:
        return self._proc

    @property
    def cmd(self) -> str:
        return self._cmd

    @property
    def is_orphan(self) -> bool:
        return False

    @property
    def started_epoch(self) -> int:
        return 0

    @property
    def reader_task(self) -> asyncio.Task[Any] | None:
        return self._reader_task

    def is_running(self) -> bool:
        return self._running

    def returncode(self) -> int | None:
        return self._proc.returncode

    def elapsed(self) -> float:
        return self._elapsed_seconds

    def read(self, tail: int = 100) -> str:
        return "\n".join(self._output[-tail:])

    def read_tail(self, tail: int = 100) -> OutputTail:
        return OutputTail.of(self._output, tail)

    def read_new(self, max_lines: int = 200) -> str:
        return ""

    def kill(self) -> None:
        self._running = False
        self._proc.kill()
