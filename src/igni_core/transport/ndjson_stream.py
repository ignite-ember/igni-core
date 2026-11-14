"""Newline-delimited-JSON stream transport base.

Owns the wire-format halves of a duplex ``StreamReader`` /
``StreamWriter`` pair — writing one JSON object per line on send,
consuming one line at a time on receive, and delegating each incoming
line to a :class:`~igni_core.protocol.registry.MessageRegistry` for
Pydantic hydration.

Subclasses (:class:`UnixSocketServerTransport`,
:class:`UnixSocketClientTransport`) supply the connection lifecycle:
how the stream pair gets bound and populated on ``self._reader`` /
``self._writer``. Everything downstream of that — framing, closing,
``is_closed`` — is shared here so a future TCP or named-pipe
stream transport can inherit the same body.

Not used by :class:`~igni_core.transport.in_process.InProcessTransport`
(queue-based, no wire framing) nor by
:class:`~igni_core.transport.websocket.WebSocketServerTransport`
(WebSocket frames self-delimit, so the newline framing here would be
double work). Both intentionally live outside this hierarchy.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from igni_core.protocol.messages import Message
from igni_core.protocol.registry import MessageRegistry
from igni_core.transport.base import Transport

# StreamReader buffer cap. asyncio's default is 64 KiB, which a single
# NDJSON message can blow past (e.g. MCP tool descriptions across several
# servers). Hitting the cap raises LimitOverrunError, killing the reader
# loop — after which every subsequent command hangs until its 60 s
# wait_for fires. 64 MiB is well clear of any realistic message.
STREAM_LIMIT = 64 * 1024 * 1024


class NDJsonStreamTransport(Transport):
    """Shared body for newline-delimited-JSON stream transports.

    Subclasses populate ``self._reader`` / ``self._writer`` from their
    own lifecycle (bind + accept for the server, connect for the
    client) and inherit ``send`` / ``receive`` / ``close`` /
    ``is_closed`` unchanged.
    """

    def __init__(self, registry: MessageRegistry | None = None) -> None:
        # Populated by subclass lifecycle (accept / connect).
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._closed = False
        self._registry = registry if registry is not None else MessageRegistry()

    async def send(self, message: Message) -> None:
        if self._writer is None or self._closed:
            return
        line = message.model_dump_json() + "\n"
        try:
            self._writer.write(line.encode())
            await self._writer.drain()
        except ConnectionError:
            # The peer is gone (reset, broken pipe): like a send with no
            # peer, the message has nowhere to go.
            return

    async def _wait_for_reader(self) -> None:
        """Hook for subclasses that need to block until ``self._reader``
        is populated (e.g. the server waits for a client to connect).

        Default: raise if the reader is not ready — clients must call
        their ``connect()`` before ``receive()``. Overridden by the
        server subclass to await its connection event.
        """
        if self._reader is None:
            raise RuntimeError("Not connected — call connect() first")

    async def receive(self) -> AsyncIterator[Message]:
        if self._reader is None:
            await self._wait_for_reader()
        assert self._reader is not None

        while not self._closed:
            line_bytes = await self._reader.readline()
            if not line_bytes:
                break  # Connection closed
            # A line that is not UTF-8 is a bad line like any other: it
            # reads as garbage the registry refuses, and the stream goes on.
            line = line_bytes.decode(errors="replace").strip()
            if not line:
                continue
            msg = self._registry.deserialize(line)
            if msg is not None:
                yield msg

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._close_writer()
        await self._close_extra()

    async def _close_writer(self) -> None:
        if self._writer:
            self._writer.close()
            # The peer may have gone first; closing is all that was asked.
            with contextlib.suppress(ConnectionError):
                await self._writer.wait_closed()

    async def _close_extra(self) -> None:
        """Hook for subclass-specific cleanup (server socket teardown,
        removing the on-disk socket file, etc.). Default: no-op."""

    @property
    def is_closed(self) -> bool:
        return self._closed
