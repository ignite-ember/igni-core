"""A session's status: told every time, the connection and account when they move."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from igni_core.extensions.host.broadcasts.signal import Emitter, Handler, Signal
from igni_core.protocol import messages as msg

STATUS_CHANGED = "chat.status_changed"
ACCOUNT_CHANGED = "account.changed"


class Connection(Signal):
    WIRE: ClassVar[str] = "app.connection_changed"

    cloud_connected: bool
    model_configured: bool

    @classmethod
    def of_status(cls, status: msg.StatusUpdate) -> Connection:
        return cls(cloud_connected=status.cloud_connected, model_configured=status.model_configured)


class StatusMemory:
    """The last status seen, by session."""

    def __init__(self) -> None:
        self._by_session: dict[str, msg.StatusUpdate] = {}

    def remember(self, status: msg.StatusUpdate) -> msg.StatusUpdate | None:
        """Keep the session's new status; the one it replaces, if any."""
        previous = self._by_session.get(status.session_id)
        self._by_session[status.session_id] = status
        return previous


class StatusSignals:
    """Translates ``status_update``: the status, and what it says moved."""

    def __init__(
        self,
        emitter: Emitter,
        memory: StatusMemory,
        status_of: Callable[[dict[str, Any], str], dict[str, Any]],
        account_of: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        self._emitter = emitter
        self._memory = memory
        self._status_of = status_of
        self._account_of = account_of

    def handlers(self) -> dict[type[msg.Message], Handler]:
        return {msg.StatusUpdate: self.status_update}

    def status_update(self, m: msg.StatusUpdate) -> None:
        previous = self._memory.remember(m)
        status = m.model_dump()
        self._emitter.value(STATUS_CHANGED, self._status_of(status, m.session_id))
        connection = Connection.of_status(m)
        account = self._account_of(status)
        if previous is None or Connection.of_status(previous) != connection:
            self._emitter.send(connection)
        if previous is None or self._account_of(previous.model_dump()) != account:
            self._emitter.value(ACCOUNT_CHANGED, account)


__all__ = ["Connection", "StatusMemory", "StatusSignals"]
