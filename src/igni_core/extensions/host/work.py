"""The host model's background work, kept until it finishes."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from igni_core.core.activity import background


class BackgroundWork:
    """Tasks the host started and has not seen finish."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Run something in the background, kept until it finishes."""
        try:
            task = background.start(coro, holder="host-model", reason="host model work")
        except RuntimeError:
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for background work; for tests and shutdown."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def idle(self) -> bool:
        """Nothing is running."""
        return not self._tasks


__all__ = ["BackgroundWork"]
