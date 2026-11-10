"""The messages being handled in the background, so shutdown can let them finish."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from igni_core.core.activity import background


class InFlightTasks:
    """Owns every background dispatch until it finishes; drained on a graceful exit."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, work: Coroutine[Any, Any, Any], name: str = "dispatch") -> asyncio.Task[Any]:
        """``work`` in the background.

        Not a busy lease of its own: a message that starts a run is the
        session's to account for — running, or waiting on a person, which
        must not count as busy — and anything else it does is short.
        """
        task = background.daemon(work, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def pending(self) -> list[asyncio.Task[Any]]:
        """The ones not finished yet."""
        return [task for task in self._tasks if not task.done()]

    async def drain(self) -> None:
        """Wait for every one, so no mid-stream message is dropped on the way out."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
