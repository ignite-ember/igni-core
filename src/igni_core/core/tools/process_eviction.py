"""Per-pid eviction of finished processes.

Split out of :mod:`igni_core.core.tools.process_registry` so the
registry holds the processes and this holds the timers that take
finished ones out of it. Two concerns, two classes: the registry
owns the pid map, this owns the TTL knob and the task handles.

The task body lives here; what "evicting" means does not — the
registry hands in the callback the task runs when the TTL expires,
so the map stays in one place and the timer stays in another.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

from igni_core.core.activity import background


class ProcessEviction:
    """Owns the finished-process TTL and the per-pid eviction tasks.

    One instance per registry. The TTL is read fresh on every
    :meth:`arm`, so a change to it takes effect on the next arm
    rather than on the next restart.
    """

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl_seconds = ttl_seconds
        self._tasks: dict[int, asyncio.Task[Any]] = {}
        self._lock = threading.Lock()

    @property
    def ttl_seconds(self) -> float:
        """Current finished-process eviction TTL."""
        return self._ttl_seconds

    def set_ttl_seconds(self, seconds: float) -> None:
        """Override the finished-process eviction TTL."""
        self._ttl_seconds = seconds

    def arm(self, pid: int, expire: Callable[[], None]) -> None:
        """Arm (or refresh) the eviction task for ``pid``.

        Cancels the task already armed for the pid first, so the TTL
        counts from the most recent arm rather than from the first
        one — the point of the TTL is "ten minutes after the last
        time anyone looked at this".

        ``expire`` is what the task calls when the TTL runs out. It
        runs on the event loop, not under this class's lock.
        """
        self.cancel(pid)
        ttl = self._ttl_seconds

        async def _evict() -> None:
            try:
                await asyncio.sleep(ttl)
            except asyncio.CancelledError:
                return
            expire()

        with self._lock:
            self._tasks[pid] = background.daemon(_evict(), name=f"process-evict-{pid}")

    def forget(self, pid: int) -> None:
        """Drop the task handle for ``pid`` without cancelling it.

        The expiry path uses this: the task is done, so there is
        nothing left to cancel.
        """
        with self._lock:
            self._tasks.pop(pid, None)

    def cancel(self, pid: int) -> None:
        """Cancel the eviction task for ``pid`` and forget it."""
        with self._lock:
            task = self._tasks.pop(pid, None)
        if task is not None and not task.done():
            task.cancel()

    def cancel_all(self) -> None:
        """Cancel every armed task and forget them all."""
        with self._lock:
            tasks = list(self._tasks.values())
            self._tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
