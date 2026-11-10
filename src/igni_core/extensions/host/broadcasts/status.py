"""A session's status: told every time, the connection and account when they move."""

from __future__ import annotations

from typing import ClassVar

from igni_core.extensions.host.broadcasts.signal import Handler
from igni_core.extensions.host.signal_values import Emitter, Signal
from igni_core.extensions.host.state.status import StatusMemory
from igni_core.extensions.host.values import Account, SessionStatus
from igni_core.protocol import messages as msg


class Connection(Signal):
    WIRE: ClassVar[str] = "app.connection_changed"

    cloud_connected: bool
    model_configured: bool

    @classmethod
    def of_status(cls, status: msg.StatusUpdate) -> Connection:
        return cls(cloud_connected=status.cloud_connected, model_configured=status.model_configured)


class StatusSignals:
    """Translates ``status_update``: the status, and what it says moved."""

    def __init__(self, emitter: Emitter, memory: StatusMemory) -> None:
        self._emitter = emitter
        self._memory = memory

    def handlers(self) -> dict[type[msg.Message], Handler]:
        return {msg.StatusUpdate: self.status_update}

    def status_update(self, m: msg.StatusUpdate) -> None:
        previous = self._memory.remember(m)
        self._emitter.send(SessionStatus.from_update(m))
        connection = Connection.of_status(m)
        account = Account.from_update(m)
        if previous is None or Connection.of_status(previous) != connection:
            self._emitter.send(connection)
        if previous is None or Account.from_update(previous) != account:
            self._emitter.send(account)


__all__ = ["Connection", "StatusSignals"]
