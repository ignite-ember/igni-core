"""A window's model changing, told as ``ui.*`` and ``windows.*`` signals.

:class:`~.windows.Windows` tells every change to its listeners; this one
turns each into the signal the host schema publishes for that field —
the same signal whether the person moved the window or an extension
wrote ``host.ui``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field

from igni_core.extensions.host.signal_values import Emitter, Windowed
from igni_core.extensions.host.window_changes import WindowChange
from igni_core.extensions.host.window_values import (
    Composer,
    Dialog,
    Page,
    Panel,
    Preview,
    Scroll,
    Selection,
    Sidebar,
    Toast,
    WindowInfo,
)

SessionSwitched = Callable[[str, str | None], None]
ChangeHandler = Callable[[WindowChange], None]


class Navigated(Windowed):
    WIRE: ClassVar[str] = "ui.navigated"

    window: str
    pages: list[Page]


class WindowFieldChanged(Windowed):
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

    panel: Panel | None = None


class SidebarChanged(WindowFieldChanged):
    WIRE: ClassVar[str] = "ui.sidebar_changed"
    FIELD: ClassVar[str] = "sidebar"

    sidebar: Sidebar


class PreviewChanged(WindowFieldChanged):
    WIRE: ClassVar[str] = "ui.preview_changed"
    FIELD: ClassVar[str] = "preview"

    preview: Preview | None = None


class SelectionChanged(WindowFieldChanged):
    WIRE: ClassVar[str] = "ui.selection_changed"
    FIELD: ClassVar[str] = "selection"

    selection: Selection


class ComposerChanged(WindowFieldChanged):
    WIRE: ClassVar[str] = "ui.composer_changed"
    FIELD: ClassVar[str] = "composer"

    composer: Composer


class Scrolled(Windowed):
    """The scroll as the window wrote it, and which window."""

    WIRE: ClassVar[str] = "ui.scrolled"

    #: A window's scroll may carry more than the schema names; it is told whole.
    model_config = ConfigDict(extra="allow")

    message_id: str | None = None
    at_bottom: bool
    window: str | None = None

    @classmethod
    def of_scroll(cls, window: str, scroll: Scroll) -> Scrolled:
        """The scroll as the window wrote it, and which window it is."""
        return cls.model_validate({**scroll.model_dump(), "window": window})


class ToastChanged(Windowed):
    """A toast shown or dismissed, and in which window."""

    window: str
    toast: Toast | None


class ToastShown(ToastChanged):
    WIRE: ClassVar[str] = "ui.toast_shown"


class ToastDismissed(ToastChanged):
    WIRE: ClassVar[str] = "ui.toast_dismissed"


class DialogOpened(Windowed):
    WIRE: ClassVar[str] = "ui.dialog_opened"

    window: str
    dialog: Dialog | None
    #: An open dialog has no answer yet: the schema's field, never told.
    answer: Any = Field(default=None, exclude=True)


class DialogClosed(Windowed):
    WIRE: ClassVar[str] = "ui.dialog_closed"

    window: str
    dialog: Dialog | None
    #: The person's answer; ``None`` when dismissed — told either way.
    answer: Any = None


class SignalRoutes(BaseModel):
    """Which signal each change is told as.

    The tables live here, so the router keeps one thing: what to send.
    """

    #: The window-model field, and the signal it publishes.
    fields: dict[str, type[WindowFieldChanged]] = Field(default_factory=dict)
    #: The toast event, and the signal it publishes.
    toasts: dict[str, type[ToastChanged]] = Field(default_factory=dict)
    #: ``Windows.on_window``'s ``what``, and the wire it is told on.
    windows: dict[str, str] = Field(default_factory=dict)


class WindowSignals:
    """Routes each window change to the signal its field publishes.

    The routing tables are the instance's own: which signal each field
    publishes, which signal each toast event is told as, and the wire
    each window event is told on.
    """

    def __init__(self, emitter: Emitter, session_switched: SessionSwitched) -> None:
        self._emitter = emitter
        self._session_switched = session_switched
        self._routes = SignalRoutes(
            fields={
                signal.FIELD: signal
                for signal in (
                    PanelChanged,
                    SidebarChanged,
                    PreviewChanged,
                    SelectionChanged,
                    ComposerChanged,
                )
            },
            toasts={
                "toast_shown": ToastShown,
                "toast_dismissed": ToastDismissed,
            },
            windows={
                "opened": "windows.opened",
                "closed": "windows.closed",
                "focus_changed": "windows.focus_changed",
            },
        )
        self._table: dict[str, ChangeHandler] = {
            "session": self._session,
            "route": self._route,
            "scroll": self._scroll,
            "toasts": self._toasts,
            "dialog": self._dialog,
            **dict.fromkeys(self._routes.fields, self._field),
        }

    def fields(self) -> frozenset[str]:
        """Every window-model field with a signal, for the coverage test."""
        return frozenset(self._table)

    def changed(self, change: WindowChange) -> None:
        handler = self._table.get(change.field)
        if handler is not None:
            handler(change)

    def window_moved(self, what: str, window: WindowInfo) -> None:
        """A window opened, closed or took the focus."""
        self._emitter.value(self._routes.windows[what], window)

    def _session(self, change: WindowChange) -> None:
        if change.value:
            self._session_switched(str(change.value), change.window)

    def _route(self, change: WindowChange) -> None:
        self._emitter.send(Navigated(window=change.window, pages=change.value))

    def _field(self, change: WindowChange) -> None:
        self._emitter.send(self._routes.fields[change.field].of_value(change.window, change.value))

    def _scroll(self, change: WindowChange) -> None:
        self._emitter.send(Scrolled.of_scroll(change.window, change.value))

    def _toasts(self, change: WindowChange) -> None:
        signal = self._routes.toasts.get(change.event)
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
