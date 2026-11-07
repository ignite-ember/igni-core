"""A window's model changing, told as ``ui.*`` and ``windows.*`` signals.

:class:`~.windows.Windows` tells every change to its listeners; this one
turns each into the signal the host schema publishes for that field —
the same signal whether the person moved the window or an extension
wrote ``host.ui``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar, Self

from pydantic import ConfigDict, Field

from igni_core.extensions.host.signal_values import Emitter, Signal
from igni_core.extensions.host.windows import WindowChange

SessionSwitched = Callable[[str, str | None], None]
ChangeHandler = Callable[[WindowChange], None]


class Navigated(Signal):
    WIRE: ClassVar[str] = "ui.navigated"

    window: str
    pages: Any


class WindowFieldChanged(Signal):
    """``{window, <field>: value}`` — a field whose signal carries its new value."""

    #: The window-model field, and the key its value is told under.
    FIELD: ClassVar[str]

    window: str

    @classmethod
    def of_value(cls, window: str, value: Any) -> Self:
        return cls.model_validate({"window": window, cls.FIELD: value})


class PanelChanged(WindowFieldChanged):
    WIRE: ClassVar[str] = "ui.panel_changed"
    FIELD: ClassVar[str] = "panel"

    panel: Any = None


class SidebarChanged(WindowFieldChanged):
    WIRE: ClassVar[str] = "ui.sidebar_changed"
    FIELD: ClassVar[str] = "sidebar"

    sidebar: Any


class PreviewChanged(WindowFieldChanged):
    WIRE: ClassVar[str] = "ui.preview_changed"
    FIELD: ClassVar[str] = "preview"

    preview: Any = None


class SelectionChanged(WindowFieldChanged):
    WIRE: ClassVar[str] = "ui.selection_changed"
    FIELD: ClassVar[str] = "selection"

    selection: Any


class ComposerChanged(WindowFieldChanged):
    WIRE: ClassVar[str] = "ui.composer_changed"
    FIELD: ClassVar[str] = "composer"

    composer: Any


class Scrolled(Signal):
    """The scroll as the window wrote it, and which window."""

    WIRE: ClassVar[str] = "ui.scrolled"

    #: A window's scroll may carry more than the schema names; it is told whole.
    model_config = ConfigDict(extra="allow")

    message_id: str | None = None
    at_bottom: bool
    window: str | None = None


class ToastChanged(Signal):
    """A toast shown or dismissed, and in which window."""

    window: str
    toast: dict[str, Any] | None


class ToastShown(ToastChanged):
    WIRE: ClassVar[str] = "ui.toast_shown"


class ToastDismissed(ToastChanged):
    WIRE: ClassVar[str] = "ui.toast_dismissed"


class DialogOpened(Signal):
    WIRE: ClassVar[str] = "ui.dialog_opened"

    window: str
    dialog: dict[str, Any] | None
    #: An open dialog has no answer yet: the schema's field, never told.
    answer: Any = Field(default=None, exclude=True)


class DialogClosed(Signal):
    WIRE: ClassVar[str] = "ui.dialog_closed"

    window: str
    dialog: dict[str, Any] | None
    #: The person's answer; ``None`` when dismissed — told either way.
    answer: Any = None


class WindowSignals:
    """Routes each window change to the signal its field publishes."""

    FIELD_SIGNALS: ClassVar[dict[str, type[WindowFieldChanged]]] = {
        signal.FIELD: signal
        for signal in (
            PanelChanged,
            SidebarChanged,
            PreviewChanged,
            SelectionChanged,
            ComposerChanged,
        )
    }
    TOAST_SIGNALS: ClassVar[dict[str, type[ToastChanged]]] = {
        "toast_shown": ToastShown,
        "toast_dismissed": ToastDismissed,
    }
    #: ``Windows.on_window``'s ``what``, and the signal it is told as.
    WINDOW_WIRES: ClassVar[dict[str, str]] = {
        "opened": "windows.opened",
        "closed": "windows.closed",
        "focus_changed": "windows.focus_changed",
    }

    def __init__(self, emitter: Emitter, session_switched: SessionSwitched) -> None:
        self._emitter = emitter
        self._session_switched = session_switched
        self._table: dict[str, ChangeHandler] = {
            "session": self._session,
            "route": self._route,
            "scroll": self._scroll,
            "toasts": self._toasts,
            "dialog": self._dialog,
            **dict.fromkeys(self.FIELD_SIGNALS, self._field),
        }

    def fields(self) -> frozenset[str]:
        """Every window-model field with a signal, for the coverage test."""
        return frozenset(self._table)

    def changed(self, change: WindowChange) -> None:
        handler = self._table.get(change.field)
        if handler is not None:
            handler(change)

    def window_moved(self, what: str, window: dict[str, Any]) -> None:
        """A window opened, closed or took the focus."""
        self._emitter.value(self.WINDOW_WIRES[what], window)

    def _session(self, change: WindowChange) -> None:
        if change.value:
            self._session_switched(str(change.value), change.window)

    def _route(self, change: WindowChange) -> None:
        self._emitter.send(Navigated(window=change.window, pages=change.value))

    def _field(self, change: WindowChange) -> None:
        self._emitter.send(self.FIELD_SIGNALS[change.field].of_value(change.window, change.value))

    def _scroll(self, change: WindowChange) -> None:
        self._emitter.send(Scrolled.model_validate({**change.value, "window": change.window}))

    def _toasts(self, change: WindowChange) -> None:
        signal = self.TOAST_SIGNALS.get(change.event)
        if signal is not None:
            self._emitter.send(signal(window=change.window, toast=change.item))

    def _dialog(self, change: WindowChange) -> None:
        if change.event == "dialog_opened":
            self._emitter.send(DialogOpened(window=change.window, dialog=change.item))
        elif change.event == "dialog_closed":
            self._emitter.send(
                DialogClosed(window=change.window, dialog=change.item, answer=change.answer)
            )


__all__ = [
    "ComposerChanged",
    "DialogClosed",
    "DialogOpened",
    "Navigated",
    "PanelChanged",
    "PreviewChanged",
    "Scrolled",
    "SelectionChanged",
    "SidebarChanged",
    "ToastDismissed",
    "ToastShown",
    "WindowFieldChanged",
    "WindowSignals",
]
