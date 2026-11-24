"""The stores in the global database, built when first asked for.

Not when a session's controllers are wired: the settings a test hands in
need not name a real database, and a store nobody asks of never needs one.
"""

from __future__ import annotations

from functools import cached_property

from igni_core.core.config.settings import Settings
from igni_core.core.db.locator import StorageLocator
from igni_core.core.session.session_directories import SessionDirectoryStore
from igni_core.core.session.session_reads import SessionReadStore


class GlobalStores:
    """The global database's stores, each built from the settings on first use."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @cached_property
    def _locator(self) -> StorageLocator:
        return StorageLocator.from_settings(self._settings)

    @cached_property
    def session_directories(self) -> SessionDirectoryStore:
        return SessionDirectoryStore.from_locator(self._locator)

    @cached_property
    def session_reads(self) -> SessionReadStore:
        return SessionReadStore.from_locator(self._locator)

    def directory_of(self, session_id: str) -> str | None:
        """The project folder ``session_id`` is bound to, or ``None``."""
        return self.session_directories.get_dir(session_id)

    def unread_sessions(self) -> set[str]:
        """The chats with a finished run the person has not looked at."""
        return self.session_reads.unread()


__all__ = ["GlobalStores"]
