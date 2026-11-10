"""Abstract transport interface for BE↔FE communication."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable

from igni_core.protocol.messages import Message


class Transport(ABC):
    """Bidirectional message transport between backend and frontend.

    Implementations must handle serialization/deserialization of
    protocol messages to/from the underlying medium.
    """

    @abstractmethod
    async def send(self, message: Message) -> None:
        """Send a protocol message to the other side."""

    @abstractmethod
    def receive(self) -> AsyncIterator[Message]:
        """Receive protocol messages from the other side.

        Returns an async iterator that yields messages as they arrive.
        Raises StopAsyncIteration when the connection is closed.
        """

    @abstractmethod
    async def close(self) -> None:
        """Close the transport gracefully."""

    @property
    @abstractmethod
    def is_closed(self) -> bool:
        """Whether the transport has been closed."""


class ClientAwareTransport(Transport):
    """A :class:`Transport` that serves client connections and names them.

    A view is a client connection: the socket's one frontend, or each
    WebSocket tab. A transport that knows its views can say how many are
    attached, tell a watcher when one comes or goes, and say whether a
    given client id is one of its own — which is what routes a reply
    addressed to a single client (``Message.to_client``).

    Making this an explicit ABC lets
    :class:`~igni_core.transport.composite.CompositeTransport` dispatch
    to the children that know about their clients via ``isinstance``
    instead of duck-typing them with ``getattr`` / ``hasattr``. Pure
    client-side transports (``NDJsonStreamTransport`` under
    ``UnixSocketClientTransport``, ``InProcessTransport``) stay on plain
    :class:`Transport`: they carry messages, not connections.
    """

    @property
    @abstractmethod
    def client_count(self) -> int:
        """How many clients are connected now."""

    def watch_connections(self, watcher: Callable[[str, str], None]) -> None:
        """Be told ``("attached" | "detached", client_id)`` for every view.

        A transport that tells no views apart (one client on a socket)
        reports none; one that does overrides this.
        """

    @abstractmethod
    def owns_client(self, client_id: str) -> bool:
        """Whether ``client_id`` names a connection of this transport.

        A transport whose single connection carries no id (the Unix
        socket) owns no named client.
        """


class ListeningTransport(ClientAwareTransport):
    """A :class:`Transport` that owns a server socket lifecycle.

    Server-side transports (Unix socket, WebSocket) need two extra
    lifecycle hooks beyond the base ``send`` / ``receive`` / ``close``
    contract: ``start()`` binds the listening socket, and
    ``wait_for_connection()`` blocks until the first client attaches.

    Making this an explicit ABC lets :class:`~igni_core.transport.composite.CompositeTransport`
    dispatch to child listening transports via ``isinstance`` instead
    of duck-typing them with ``getattr`` / ``hasattr`` — pure
    client-side transports (``NDJsonStreamTransport`` under
    ``UnixSocketClientTransport``, ``InProcessTransport``) stay on
    plain :class:`Transport` and are not asked to grow spurious
    ``start`` / ``wait_for_connection`` methods. The client-facing half
    of the contract lives on :class:`ClientAwareTransport`.
    """

    @abstractmethod
    async def start(self) -> None:
        """Bind the listening socket; must be awaited before ``send`` / ``receive``."""

    @abstractmethod
    async def wait_for_connection(self, timeout: float | None = 30.0) -> None:
        """Block until the first client attaches (or ``timeout`` expires)."""
