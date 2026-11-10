"""Where a window's model is kept: what lasts, and what lives here.

The lasting fields — the page stack, the sidebar, the bound session and
folder, each session's draft — are written to the client-state store, so
they survive igni restarting. The rest lives as long as this backend
does, and survives a window reloading.
"""

from __future__ import annotations

from pydantic import BaseModel

from igni_core.core.session.client_state import ClientStateStoreProtocol
from igni_core.extensions.host.window_errors import WindowStateError
from igni_core.extensions.host.window_values import Transient


class UiKeys:
    """The keys a window's lasting fields are stored under."""

    SESSION = "session-id"
    PROJECT_DIR = "project-dir"
    PAGE = "page"
    SIDEBAR = "sidebar-open"
    SIDEBAR_WIDTH = "sidebar-width"
    SIDEBAR_SECTION = "sidebar-section"
    DRAFT_PREFIX = "draft:"
    MODE_PREFIX = "draft-mode:"

    @classmethod
    def draft(cls, session_id: str) -> str:
        return f"{cls.DRAFT_PREFIX}{session_id}"

    @classmethod
    def mode(cls, session_id: str) -> str:
        return f"{cls.MODE_PREFIX}{session_id}"


class LastingState:
    """What a window's lasting fields are kept in: the client-state store.

    A read that failed is a failure, not an empty state: the window
    would show its defaults and then write over what is really there.
    """

    def __init__(self, store: ClientStateStoreProtocol) -> None:
        self._store = store

    def read(self, window_id: str) -> dict[str, str]:
        """Everything this window has stored."""
        read = self._store.get_for_client(window_id)
        if not read.ok:
            raise WindowStateError(f"window {window_id!r} state could not be read")
        return read.values

    def write(self, window_id: str, key: str, value: str) -> None:
        """Keep one value for this window; an empty value keeps nothing."""
        result = (
            self._store.set_value(window_id, key, value)
            if value
            else self._store.delete_value(window_id, key)
        )
        if not result.ok:
            raise WindowStateError(f"window {window_id!r} state could not be written")


class TransientState:
    """The fields of every window that live in this process only."""

    def __init__(self) -> None:
        self._values: dict[str, Transient] = {}

    def of(self, window_id: str) -> Transient:
        """The transient fields of one window, as they are now."""
        found = self._values.get(window_id)
        if found is None:
            found = Transient()
            self._values[window_id] = found
        return found


class FieldContext(BaseModel):
    """One window, and what is known of it right now.

    The lasting values are read by the field that keeps them; the
    transient ones are the window's own, in this process.
    """

    window_id: str
    transient: Transient


class WindowState:
    """Where a window's model is kept: what lasts, and what lives here."""

    def __init__(self, store: ClientStateStoreProtocol) -> None:
        self._lasting = LastingState(store)
        self._transient = TransientState()

    @property
    def lasting(self) -> LastingState:
        """The fields that survive igni restarting."""
        return self._lasting

    @property
    def transient(self) -> TransientState:
        """The fields that live as long as this backend does."""
        return self._transient

    def context(self, window_id: str) -> FieldContext:
        """One window, and what is known of it right now."""
        return FieldContext(window_id=window_id, transient=self._transient.of(window_id))


__all__ = ["FieldContext", "LastingState", "TransientState", "UiKeys", "WindowState"]
