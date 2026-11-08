"""The backend's root transport, with observers on the way out.

Wrapped once, at the root: everything igni sends any window passes
through it, so the host model sees every broadcast on its way out and
turns it into signals; and an action the host sends on an extension's
behalf whose answer belongs to the extension (a slash command it ran)
is taken back here instead of being shown in the windows.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable

from igni_core.backend.transport_builder import ServerTransport
from igni_core.protocol import messages as msg
from igni_core.transport.base import ListeningTransport

logger = logging.getLogger(__name__)


class ObservedTransport(ListeningTransport):
    """The root transport, with observers on the way out."""

    def __init__(self, inner: ServerTransport) -> None:
        self._inner = inner
        self._observers: list[Callable[[msg.Message], None]] = []
        self._waiting: dict[str, asyncio.Future[msg.Message]] = {}

    @property
    def inner(self) -> ServerTransport:
        return self._inner

    def observe(self, observer: Callable[[msg.Message], None]) -> None:
        self._observers.append(observer)

    def expect(self, message_id: str) -> asyncio.Future[msg.Message]:
        """The next message sent with ``message_id`` comes here, not to the windows."""
        future: asyncio.Future[msg.Message] = asyncio.get_running_loop().create_future()
        self._waiting[message_id] = future
        return future

    def forget(self, message_id: str) -> None:
        self._waiting.pop(message_id, None)

    async def send(self, message: msg.Message) -> None:
        if message.id and message.id in self._waiting:
            future = self._waiting.pop(message.id)
            if not future.done():
                future.set_result(message)
            return
        for observer in list(self._observers):
            try:
                observer(message)
            except Exception:
                logger.exception("an outbound observer failed on %s", message.type)
        await self._inner.send(message)

    def receive(self) -> AsyncIterator[msg.Message]:
        return self._inner.receive()

    async def close(self) -> None:
        await self._inner.close()

    @property
    def is_closed(self) -> bool:
        return self._inner.is_closed

    async def start(self) -> None:
        await self._inner.start()

    async def wait_for_connection(self, timeout: float | None = 30.0) -> None:
        await self._inner.wait_for_connection(timeout=timeout)

    @property
    def client_count(self) -> int:
        """Clients connected to the root transport now."""
        return self._inner.client_count

    def watch_connections(self, watcher: Callable[[str, str], None]) -> None:
        """Be told of every view attaching to or detaching from the root transport."""
        self._inner.watch_connections(watcher)


__all__ = ["ObservedTransport"]
