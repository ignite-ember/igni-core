"""Fan one BE across several transports (e.g. Unix socket for the TUI +
WebSocket for GUI tabs) so every attached view mirrors the same session.

Lives in its own module — not co-located with :mod:`igni_core.transport.websocket`
because the composite fan-out is transport-agnostic, not WS-specific.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable

from igni_core.core.activity import background
from igni_core.protocol.messages import Message
from igni_core.transport.base import ClientAwareTransport, ListeningTransport, Transport

logger = logging.getLogger(__name__)


class CompositeTransport(ListeningTransport):
    """Fans one BE across several transports (e.g. unix for the TUI +
    WS for GUI tabs) so every attached view mirrors the same session.

    ``send()`` broadcasts to every child; ``receive()`` merges every
    child's inbound stream. A child closing does NOT end the merged
    stream — only ``close()`` does.

    It is a :class:`~igni_core.transport.base.ListeningTransport` itself:
    it binds whatever its children bind and waits for whichever child
    gets a client first.
    """

    def __init__(self, transports: list[Transport]):
        self._transports = transports
        self._closed = False
        self._inbox: asyncio.Queue[Message | None] = asyncio.Queue()
        self._pumps: list[asyncio.Task] = []

    def _listening_children(self) -> list[ListeningTransport]:
        """Subset of children that own a server socket lifecycle.

        Client-only transports (``UnixSocketClientTransport``,
        ``InProcessTransport``) stay on plain :class:`Transport` and are
        skipped for ``start`` / ``wait_for_connection`` dispatch — they
        have nothing to bind and nothing to wait on.
        """
        return [t for t in self._transports if isinstance(t, ListeningTransport)]

    def _client_aware_children(self) -> list[ClientAwareTransport]:
        """Subset of children that know about the clients attached to them.

        The rest (``UnixSocketClientTransport``,
        ``InProcessTransport``) carry messages but no connections: they
        count for no client, they report no view coming or going, and
        they own no client id.
        """
        return [t for t in self._transports if isinstance(t, ClientAwareTransport)]

    async def start(self) -> None:
        for t in self._listening_children():
            await t.start()

    async def _pump(self, transport: Transport) -> None:
        """Merge one child's inbound stream into the composite's inbox."""
        try:
            async for msg in transport.receive():
                await self._inbox.put(msg)
        except Exception as exc:  # pragma: no cover — defensive
            logger.info("composite child receive ended: %s", exc)

    def _start_pumps(self) -> None:
        """Start the merge pumps — once.

        They normally start when the first client attaches; a caller
        that skips straight to ``receive()`` gets them here instead.
        """
        if self._pumps:
            return
        self._pumps = [
            background.daemon(self._pump(t), name="transport-pump") for t in self._transports
        ]

    async def wait_for_connection(self, timeout: float | None = 30.0) -> None:
        """Resolve when ANY child gets its first connection."""
        waiters = [
            background.daemon(t.wait_for_connection(timeout=None), name="transport-wait")
            for t in self._listening_children()
        ]
        if not waiters:
            return
        done, pending = await asyncio.wait(
            waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        for p in pending:
            p.cancel()
        # Let the cancelled waits land before returning: a task nobody
        # awaits is a task nobody knows is over.
        await asyncio.gather(*pending, return_exceptions=True)
        if not done:
            raise asyncio.TimeoutError
        for waiter in done:
            if not waiter.cancelled() and waiter.exception() is not None:
                logger.warning("a child transport failed while waiting: %s", waiter.exception())
        self._start_pumps()

    @property
    def client_count(self) -> int:
        """Clients connected across every child that counts them."""
        return sum(t.client_count for t in self._client_aware_children())

    def watch_connections(self, watcher: Callable[[str, str], None]) -> None:
        """Forward to every child that reports its views coming and going."""
        for t in self._client_aware_children():
            t.watch_connections(watcher)

    def owns_client(self, client_id: str) -> bool:
        """Whether any child has the connection ``client_id``."""
        return any(t.owns_client(client_id) for t in self._client_aware_children())

    async def send(self, message: Message) -> None:
        if message.to_client:
            # A reply addressed to one connection: only the child that owns
            # it gets it — never the terminal's socket, which would show it
            # to everyone there.
            for owner in self._client_aware_children():
                if owner.owns_client(message.to_client):
                    await self._send_to_child(owner, message)
                    return
            logger.debug("reply to %s dropped: no transport has that connection", message.to_client)
            return
        for t in self._transports:
            await self._send_to_child(t, message)

    async def _send_to_child(self, child: Transport, message: Message) -> None:
        """Send to one child; a child that fails must not silence the rest.

        A wedged socket is not the whole backend's problem: the other
        views still get the message, and the failure is logged rather
        than swallowed.
        """
        try:
            await child.send(message)
        except Exception:
            logger.warning(
                "composite: %s failed to send %s", type(child).__name__, message.type, exc_info=True
            )

    async def receive(self) -> AsyncIterator[Message]:
        # Pumps normally start in wait_for_connection; make sure they
        # exist even if a caller skips straight to receive().
        self._start_pumps()
        while not self._closed:
            msg = await self._inbox.get()
            if msg is None:
                break
            yield msg

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._inbox.put(None)
        for p in self._pumps:
            p.cancel()
        try:
            for outcome in await asyncio.gather(*self._pumps, return_exceptions=True):
                if isinstance(outcome, Exception):
                    logger.warning("composite: a merge pump ended badly: %s", outcome)
        finally:
            # The children are closed even if this close was cancelled:
            # a socket left open outlives the cancellation.
            for t in self._transports:
                try:
                    await t.close()
                except Exception:
                    logger.warning("composite: %s failed to close", type(t).__name__, exc_info=True)

    @property
    def is_closed(self) -> bool:
        return self._closed
