"""One change to a window's model, and who is told about it.

The change is the model's own: what changed, in which window, at which
revision. The notifier is the one place a change is told — to the
window (through the push) and to the host model's listeners (which turn
it into the signal for that field).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from igni_core.extensions.host.window_values import Dialog, Toast, WindowInfo

logger = logging.getLogger(__name__)


class WindowChange(BaseModel):
    """One change to one window's model, as listeners are told it."""

    window: str
    field: str
    value: Any = None
    #: The window's revision after the change.
    rev: int
    #: The writer's token, echoed so the window that wrote it knows its
    #: own write; empty for an extension's.
    token: str = ""
    #: For the list- and dialog-shaped fields: what happened —
    #: ``toast_shown``, ``toast_dismissed``, ``dialog_opened``,
    #: ``dialog_closed`` — and to which one.
    event: str = ""
    item: Toast | Dialog | None = None
    answer: Any = None


ChangeListener = Callable[[WindowChange], None]
WindowListener = Callable[[str, WindowInfo], None]
Push = Callable[[WindowChange], None]


class ChangeNotifier:
    """Tells everyone that a window's model changed.

    The window is told first (the push it applies), then the host
    model's listeners (which signal it). A listener that fails is
    logged, not raised: one broken listener does not stop the others
    from hearing the change.
    """

    def __init__(self, push: Push) -> None:
        self._push = push
        self._revisions: dict[str, int] = {}
        self._change_listeners: list[ChangeListener] = []
        self._window_listeners: list[WindowListener] = []

    def on_change(self, listener: ChangeListener) -> None:
        """A listener told every change to any window's model."""
        self._change_listeners.append(listener)

    def on_window(self, listener: WindowListener) -> None:
        """``listener("opened" | "closed" | "focus_changed", window)``."""
        self._window_listeners.append(listener)

    def rev(self, window_id: str) -> int:
        """The window's revision: how many changes it has had."""
        return self._revisions.get(window_id, 0)

    def announce(
        self,
        window: str,
        field: str,
        value: Any,
        *,
        token: str = "",
        event: str = "",
        item: Toast | Dialog | None = None,
        answer: Any = None,
    ) -> WindowChange:
        """The change, at the window's next revision, told to everyone."""
        rev = self._revisions.get(window, 0) + 1
        self._revisions[window] = rev
        change = WindowChange(
            window=window,
            field=field,
            value=value,
            rev=rev,
            token=token,
            event=event,
            item=item,
            answer=answer,
        )
        self._tell(change)
        return change

    def window_moved(self, what: str, window: WindowInfo) -> None:
        """A window opened, closed or took the focus."""
        for listener in list(self._window_listeners):
            try:
                listener(what, window)
            except Exception:
                logger.exception("host model: a window listener failed")

    def _tell(self, change: WindowChange) -> None:
        try:
            self._push(change)
        except Exception:
            logger.exception("host model: pushing a window change failed")
        for listener in list(self._change_listeners):
            try:
                listener(change)
            except Exception:
                logger.exception("host model: a window-change listener failed")


__all__ = [
    "ChangeListener",
    "ChangeNotifier",
    "Push",
    "WindowChange",
    "WindowListener",
]
