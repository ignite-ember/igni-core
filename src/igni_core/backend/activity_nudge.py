"""The host model told to look at the activity again when the busy registry moves."""

from __future__ import annotations

import asyncio
import threading

from igni_core.extensions.host.model import HostModel


class ActivityNudge:
    """Tells the host model to look at the activity again when the registry moves.

    A lease can be taken or let go on any thread; the model is the
    loop's. Several moves before the loop gets round to it are one look.
    """

    def __init__(self, model: HostModel, loop: asyncio.AbstractEventLoop) -> None:
        self._model = model
        self._loop = loop
        self._pending = False
        self._lock = threading.Lock()

    def __call__(self) -> None:
        with self._lock:
            if self._pending:
                return
            self._pending = True
        try:
            self._loop.call_soon_threadsafe(self._look)
        except RuntimeError:
            # The loop is closed: no look was scheduled, so none is
            # pending — a later move must not be swallowed by this one.
            with self._lock:
                self._pending = False

    def _look(self) -> None:
        with self._lock:
            self._pending = False
        self._model.services.activity_watch.moved()


__all__ = ["ActivityNudge"]
