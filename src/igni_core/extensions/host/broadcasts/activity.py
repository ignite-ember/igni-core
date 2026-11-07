"""``app.activity``: what igni is doing, told when it is not what it was."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from igni_core.extensions.host.signal_values import Emit

logger = logging.getLogger(__name__)

ACTIVITY_CHANGED = "app.activity_changed"

ActivityListener = Callable[[dict[str, Any]], None]


class ActivityRead(BaseModel):
    """The backend's activity, or why it could not be read."""

    ok: bool
    #: ``HostBackend.activity()``'s answer, as the backend gives it.
    value: dict[str, Any] | None = None
    reason: str = ""


class ActivityWatch:
    """Keeps the last activity told, and who else wants to hear of a new one."""

    def __init__(self, read: Callable[[], dict[str, Any]], emit: Emit) -> None:
        self._read_activity = read
        self._emit = emit
        self._last: dict[str, Any] | None = None
        self._listeners: list[ActivityListener] = []

    def listen(self, listener: ActivityListener) -> None:
        """Be told the new ``app.activity`` whenever ``app.activity_changed`` is."""
        self._listeners.append(listener)

    def moved(self) -> None:
        """Tell ``app.activity_changed`` when the activity is not what it was."""
        read = self._read()
        if not read.ok or read.value is None:
            logger.debug("host model: activity unavailable: %s", read.reason)
            return
        if read.value == self._last:
            return
        self._last = read.value
        self._emit(ACTIVITY_CHANGED, read.value)
        for listener in list(self._listeners):
            try:
                listener(read.value)
            except Exception:
                logger.exception("an activity listener failed")

    def _read(self) -> ActivityRead:
        try:
            return ActivityRead(ok=True, value=self._read_activity())
        except Exception as exc:
            # The backend may be going away under us; nothing to tell then.
            return ActivityRead(ok=False, reason=str(exc))


__all__ = ["ActivityRead", "ActivityWatch"]
