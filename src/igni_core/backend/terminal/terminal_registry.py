"""Every open terminal, by the connection that opened it."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from igni_core.backend.schemas_terminal import TerminalOpened
from igni_core.backend.terminal.exceptions import TerminalLimitReached
from igni_core.backend.terminal.terminal import Send, Terminal
from igni_core.backend.terminal.terminal_process import TerminalProcess
from igni_core.core.activity.background import BackgroundTasks
from igni_core.core.utils.audit import AuditEntry, AuditLogger, TerminalDetails

logger = logging.getLogger(__name__)

#: Builds the PTY a new terminal runs on: ``(cwd, cols, rows)``.
Spawn = Callable[[Path, int, int], TerminalProcess]


class TerminalRegistry:
    """Opens, finds and ends the terminals of each connection.

    A connection reaches only its own terminals: another connection's id is
    not found. When a connection goes away its terminals end, and so does
    every terminal when igni stops. Each open and each end is written to the
    audit log; what was typed or printed never is.
    """

    MAX_PER_CONNECTION = 8

    def __init__(
        self,
        *,
        send: Send,
        cwd: Path,
        audit: AuditLogger | None = None,
        spawn: Spawn = TerminalProcess.for_this_platform,
    ) -> None:
        self._send = send
        self._cwd = cwd
        self._audit = audit
        self._spawn = spawn
        self._by_connection: dict[str, dict[str, Terminal]] = {}
        self._closing: set[asyncio.Task[None]] = set()

    def count(self, client_id: str) -> int:
        """How many terminals ``client_id`` has open."""
        return len(self._by_connection.get(client_id, {}))

    def open(self, client_id: str, cols: int, rows: int) -> TerminalOpened:
        """Start a shell in the project folder for ``client_id``."""
        if self.count(client_id) >= self.MAX_PER_CONNECTION:
            raise TerminalLimitReached(
                f"a window may have at most {self.MAX_PER_CONNECTION} terminals open; "
                "close one first"
            )
        terminal = Terminal(
            terminal_id=f"term-{secrets.token_hex(8)}",
            client_id=client_id,
            process=self._spawn(self._cwd, cols, rows),
            cwd=str(self._cwd),
            send=self._send,
        )
        terminal.start(self._ended)
        self._by_connection.setdefault(client_id, {})[terminal.terminal_id] = terminal
        self._write_audit(terminal, "opened")
        return TerminalOpened(
            terminal_id=terminal.terminal_id, shell=terminal.shell, cwd=terminal.cwd
        )

    def get(self, client_id: str, terminal_id: str) -> Terminal:
        """``client_id``'s terminal ``terminal_id``; ``LookupError`` when it has none by that id."""
        terminal = self._by_connection.get(client_id, {}).get(terminal_id)
        if terminal is None:
            raise LookupError(f"no terminal {terminal_id!r} is open on this connection")
        return terminal

    def detached(self, client_id: str) -> None:
        """``client_id`` went away: end its terminals, in the background."""
        for terminal in list(self._by_connection.get(client_id, {}).values()):
            task = BackgroundTasks.shared().daemon(
                self._close_quietly(terminal), name=f"terminal-close-{terminal.terminal_id}"
            )
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)

    async def close_all(self) -> None:
        """End every terminal: igni is stopping."""
        terminals = [t for by_id in self._by_connection.values() for t in by_id.values()]
        await asyncio.gather(*(self._close_quietly(t) for t in terminals))
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)

    def connection_watcher(self, event: str, client_id: str) -> None:
        """What the transport tells of a connection: on ``detached``, its terminals end."""
        if event == "detached":
            self.detached(client_id)

    async def _close_quietly(self, terminal: Terminal) -> None:
        try:
            await terminal.close()
        except Exception:
            logger.exception("terminal %s did not close cleanly", terminal.terminal_id)

    def _ended(self, terminal: Terminal, exit_code: int | None) -> None:
        by_id = self._by_connection.get(terminal.client_id, {})
        if by_id.pop(terminal.terminal_id, None) is None:
            return
        if not by_id:
            self._by_connection.pop(terminal.client_id, None)
        self._write_audit(terminal, "closed", exit_code=exit_code)

    def _write_audit(
        self,
        terminal: Terminal,
        action: Literal["opened", "closed"],
        *,
        exit_code: int | None = None,
    ) -> None:
        if self._audit is None:
            return
        closed = action == "closed"
        details = TerminalDetails(
            action=action,
            terminal_id=terminal.terminal_id,
            shell=terminal.shell,
            cwd=terminal.cwd,
            exit_code=exit_code if closed else None,
            duration_s=round(time.monotonic() - terminal.opened_at, 3) if closed else None,
        )
        self._audit.log(AuditEntry.terminal(details))


__all__ = ["Spawn", "TerminalRegistry"]
