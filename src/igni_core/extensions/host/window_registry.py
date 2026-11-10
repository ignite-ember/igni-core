"""The windows this backend has seen: which are open, which is active,
which has focus.

A window is a view of igni — a desktop window, an IDE panel, a browser
tab — with a stable id that survives reloads. This is the list of them.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from igni_core.extensions.host.window_changes import ChangeNotifier
from igni_core.extensions.host.window_field import WindowFieldName
from igni_core.extensions.host.window_state import LastingState, TransientState, UiKeys
from igni_core.extensions.host.window_values import WindowInfo


class WindowRecord(BaseModel):
    """One window this backend has seen."""

    id: str
    kind: str = "web"
    connection_id: str = ""
    connected: bool = True


class WindowCursor(BaseModel):
    """Which window is which, when there is more than one.

    The active window is the one the person last did something in; the
    focused one is the one that says it has focus.
    """

    #: The window the person last did something in.
    active: str | None = None
    #: The window that says it has focus.
    focused: str | None = None
    #: When each window was last used, so the least recent can be found.
    last_used: dict[str, int] = Field(default_factory=dict)
    #: The count that times them.
    tick: int = 0

    def use(self, window_id: str) -> None:
        """This window was used now: it is the active one."""
        self.tick += 1
        self.last_used[window_id] = self.tick
        self.active = window_id


class WindowRegistry:
    """Every window this backend has seen, and which one is which.

    The active window is the one the person last did something in.
    ``host.ui`` addresses it. The focused window is the one that says it
    has focus; when one does, it is also the active one.
    """

    def __init__(
        self, notifier: ChangeNotifier, lasting: LastingState, transient: TransientState
    ) -> None:
        self._notifier = notifier
        self._lasting = lasting
        self._transient = transient
        self._windows: dict[str, WindowRecord] = {}
        self._cursor = WindowCursor()

    def attached(self, window_id: str, *, connection_id: str = "", kind: str = "") -> None:
        """A window hydrated its model on connect: it is open."""
        if not window_id:
            return
        record = self._windows.get(window_id)
        opened = record is None or not record.connected
        if record is None:
            record = WindowRecord(id=window_id)
            self._windows[window_id] = record
        record.connected = True
        if connection_id:
            record.connection_id = connection_id
        if kind:
            record.kind = kind
        self.touch(window_id)
        if opened:
            self._notifier.window_moved("opened", self.describe(window_id))

    def detached(self, connection_id: str) -> None:
        """A connection closed; the window on it is gone."""
        for record in self._windows.values():
            if record.connection_id == connection_id and record.connected:
                record.connected = False
                if self._cursor.active == record.id:
                    self._cursor.active = None
                if self._cursor.focused == record.id:
                    self._cursor.focused = None
                    self._transient.of(record.id).focused = False
                self._notifier.window_moved("closed", self.describe(record.id))

    def touch(self, window_id: str) -> None:
        """The person did something in this window: it is the active one."""
        if window_id in self._windows:
            self._cursor.use(window_id)

    def touch_connection(self, connection_id: str) -> None:
        """The person did something on a connection: its window is active."""
        window = self.by_connection(connection_id)
        if window is not None:
            self.touch(window)

    def by_connection(self, connection_id: str) -> str | None:
        """The window on this connection, if it is open."""
        for record in self._windows.values():
            if record.connection_id == connection_id and record.connected:
                return record.id
        return None

    def known(self, window_id: str) -> bool:
        """Whether this window is open here."""
        record = self._windows.get(window_id)
        return record is not None and record.connected

    def active(self) -> str | None:
        """The window the person last used, of those still open."""
        if self._cursor.active is not None and self._windows[self._cursor.active].connected:
            return self._cursor.active
        open_windows = self.open()
        if not open_windows:
            return None
        return max(open_windows, key=lambda r: self._cursor.last_used.get(r.id, -1)).id

    def focused(self) -> str | None:
        """The window that says it has focus, if one does."""
        return self._cursor.focused

    def focus_window(self, window_id: str, focused: bool) -> None:
        """This window says it has the focus, or that it has not.

        Only one window has the focus: the one that says so, and the
        other is told it has lost it.
        """
        before = self._cursor.focused
        if focused:
            if before is not None and before != window_id:
                self._transient.of(before).focused = False
                self._notifier.announce(before, WindowFieldName.FOCUSED.value, False)
            self._cursor.focused = window_id
            self.touch(window_id)
        elif before == window_id:
            self._cursor.focused = None
        if self._cursor.focused != before:
            self._notifier.window_moved("focus_changed", self.describe(window_id))

    def describe(self, window_id: str) -> WindowInfo:
        """One window, as the schema's ``WindowInfo`` names it."""
        record = self._windows[window_id]
        stored = self._lasting.read(window_id)
        return WindowInfo(
            id=record.id,
            kind=record.kind,
            connected=record.connected,
            focused=self._cursor.focused == record.id,
            session_id=stored.get(UiKeys.SESSION) or None,
            project_dir=stored.get(UiKeys.PROJECT_DIR) or None,
        )

    def describe_all(self) -> list[WindowInfo]:
        """Every open window, as the schema's ``WindowInfo`` names it."""
        return [self.describe(record.id) for record in self.open()]

    def open(self) -> list[WindowRecord]:
        """The windows that are open here."""
        return [record for record in self._windows.values() if record.connected]

    def record(self, window_id: str) -> WindowRecord:
        """One window's record."""
        return self._windows[window_id]


__all__ = ["WindowCursor", "WindowRecord", "WindowRegistry"]
