"""Where the values a change implies are read again, for a session.

:class:`~.deferred_reads.DeferredReads` asks a :class:`SessionValueSource`;
:class:`HostSessionValues` is the one the host model gives it, over the
gateway, the backend and the ``app.display`` reader.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from igni_core.extensions.host.backend import HostBackend
from igni_core.extensions.host.backend_gateway import HostBackendGateway
from igni_core.protocol.rpc import RpcMethod

#: Reads ``app.display`` for a session, as
#: :meth:`~igni_core.extensions.host.members.app.AppMembers.display_value`
#: does, as a :class:`~igni_core.extensions.host.members.answers.Display`.
DisplayRead = Callable[[str], Awaitable[Any]]


class SessionValueSource(Protocol):
    """How to read each value again, for a session."""

    async def account(self, session_id: str) -> Any:
        """``Account``."""
        ...

    async def display(self, session_id: str) -> Any:
        """``Display``."""
        ...

    async def loop(self, session_id: str) -> Any:
        """``LoopStatus``."""
        ...

    def default_session(self) -> str:
        """The session to read when the change named none."""
        ...


class HostSessionValues:
    """Each value read where the host keeps it.

    The backend is asked each time, not captured when built: a test
    replaces its methods under a running model.
    """

    def __init__(
        self, gateway: HostBackendGateway, backend: HostBackend, display: DisplayRead
    ) -> None:
        self._gateway = gateway
        self._backend = backend
        self._display = display

    async def account(self, session_id: str) -> Any:
        """As :meth:`HostBackendGateway.account` reads it."""
        return await self._gateway.account(session_id)

    async def display(self, session_id: str) -> Any:
        return await self._display(session_id)

    async def loop(self, session_id: str) -> Any:
        """From the backend's ``loop_status``."""
        return await self._backend.rpc(RpcMethod.LOOP_STATUS, {}, session_id=session_id)

    def default_session(self) -> str:
        return self._backend.default_session()


__all__ = ["DisplayRead", "HostSessionValues", "SessionValueSource"]
