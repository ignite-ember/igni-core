"""Which MCP servers are waiting on a person, so their calls are not timed out.

An extension's tool can wait for the person: ``ext.host.ui.show_dialog``
"waits for the person, with no timeout" (igni-extension README). The tool
call igni made into that extension has a deadline, the MCP session's read
timeout, so a person who took longer than it to answer got "Request
'tools/call' timed out" and their answer was lost. The host holds a wait
here while a dialog is open, keyed by the server's name; the session that
made the call stops counting its deadline while one is held
(:class:`~igni_core.core.mcp.request_meta.RequestMetaSession`).
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import anyio


class PersonWaits:
    """Open waits on a person, and the calls they stop the clock of, by server name."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open: dict[str, int] = {}
        #: The deadlines of calls in flight: ``(scope, seconds)`` by server.
        self._calls: dict[str, list[tuple[anyio.CancelScope, float]]] = {}

    @contextmanager
    def hold(self, server: str) -> Iterator[None]:
        """``server`` is waiting on the person while the block is open.

        The deadline of each of its calls in flight stops; once the last
        wait ends, each starts again from its full length.
        """
        with self._lock:
            self._open[server] = self._open.get(server, 0) + 1
            calls = list(self._calls.get(server, ()))
        for scope, _seconds in calls:
            scope.deadline = math.inf
        try:
            yield
        finally:
            with self._lock:
                left = self._open.get(server, 1) - 1
                if left > 0:
                    self._open[server] = left
                else:
                    self._open.pop(server, None)
                calls = [] if left > 0 else list(self._calls.get(server, ()))
            for scope, seconds in calls:
                scope.deadline = anyio.current_time() + seconds

    @contextmanager
    def timed(self, server: str, seconds: float) -> Iterator[anyio.CancelScope]:
        """A deadline of ``seconds`` for a call into ``server``, stopped
        while the server waits on the person. Check ``cancelled_caught``."""
        with anyio.CancelScope() as scope:
            with self._lock:
                waiting = server in self._open
                self._calls.setdefault(server, []).append((scope, seconds))
            scope.deadline = math.inf if waiting else anyio.current_time() + seconds
            try:
                yield scope
            finally:
                with self._lock:
                    calls = self._calls.get(server, [])
                    calls[:] = [entry for entry in calls if entry[0] is not scope]
                    if not calls:
                        self._calls.pop(server, None)

    def waiting(self, server: str) -> bool:
        """Whether ``server`` is waiting on the person now."""
        with self._lock:
            return server in self._open


#: The process's waits: the host holds them, the MCP sessions read them.
person_waits = PersonWaits()


__all__ = ["PersonWaits", "person_waits"]
