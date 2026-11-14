"""WebSocket transport — one JSON protocol message per text frame.

Serves browser-based frontends (the shared web UI: the web client
and the Tauri app). Wire format
is the same JSON the Unix-socket transport uses, except WebSocket
frames already delimit messages so no newline framing is needed.

Multi-client session mirroring: the BE owns ONE session; every
connected client is a live view of it. ``send()`` broadcasts to all
attached clients; each client gets a ``Welcome`` with its
``client_id`` at attach so it can recognise its own echoes. Clients
self-identify on inbound messages that need attribution (typing,
message echoes) by stamping that id.

Other properties callers should know about:

* **Reconnect-friendly.** ``receive()`` does NOT terminate when a
  client drops — webviews reload at will. The BE only exits via
  ``Shutdown``, signals, or the parent watchdog.
* **Loopback only by default.** The BE executes arbitrary tool
  calls; it must never listen on a routable interface.
* **Checked at the handshake.** Loopback is not enough: any web page
  and any local process can reach ``127.0.0.1``. A foreign ``Origin``
  is refused, and when the launcher gave a per-launch token every
  connection must offer it (see :mod:`igni_core.transport.ws_auth`).
* **A chunk trace, on request.** With ``IGNI_CHUNK_TRACE=1`` every
  chunk the transport broadcasts leaves a line in
  ``~/.igni/chunk_trace.log``, so an operator can see whether the
  backend streams every chunk (see
  :mod:`igni_core.transport.chunk_trace`).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING

from websockets.asyncio.server import serve

from igni_core.protocol.registry import MessageRegistry
from igni_core.protocol.schemas.be_events import ContentDelta
from igni_core.protocol.schemas.envelope import Message
from igni_core.protocol.schemas.fe_actions import StreamEnd
from igni_core.protocol.schemas.mirroring import Welcome
from igni_core.transport._ws_client import MirroredClient, SendResult
from igni_core.transport.base import ListeningTransport
from igni_core.transport.chunk_trace import ChunkTrace
from igni_core.transport.ws_auth import HandshakeGuard, Standing

if TYPE_CHECKING:
    from websockets.asyncio.server import Server, ServerConnection
    from websockets.http11 import Request

__all__ = ["WebSocketServerTransport"]

logger = logging.getLogger(__name__)

# Mirror the Unix transport's frame cap — a single message can carry
# MCP tool catalogues or large tool results.
_MAX_FRAME_BYTES = 64 * 1024 * 1024

# Per-client send timeout during broadcast. A slow or wedged client
# must not block every other attached view: after this deadline the
# offending conn is dropped from ``_conns`` and broadcast continues
# without it. 2s is plenty for any healthy localhost websocket; a
# client that misses it has either crashed mid-frame or backed up its
# OS buffer enough that recovery is unlikely.
_BROADCAST_SEND_TIMEOUT = 2.0


class WebSocketServerTransport(ListeningTransport):
    """BE-side transport: loopback WS server, N mirrored clients."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        broadcast_send_timeout: float = _BROADCAST_SEND_TIMEOUT,
        registry: MessageRegistry | None = None,
        token: str | None = None,
        chunk_trace: ChunkTrace | None = None,
    ):
        self._host = host
        # Origin always; the token too when the launcher gave one.
        self._guard = HandshakeGuard(token)
        self._port = port
        self._broadcast_send_timeout = broadcast_send_timeout
        self._registry = registry if registry is not None else MessageRegistry()
        # The operator's chunk trace. Off unless IGNI_CHUNK_TRACE=1; a
        # caller with its own trace (a test) hands one in.
        self._chunk_trace = (
            chunk_trace if chunk_trace is not None else ChunkTrace.from_environment()
        )
        # Set by ``start()`` to the awaited ``serve(...)`` result.
        self._server: Server | None = None
        # client_id → live client wrapper. Insertion order preserved so
        # broadcast order is stable (oldest first).
        self._conns: dict[str, MirroredClient] = {}
        # client_id → what that connection may do, from its handshake.
        self._standings: dict[str, Standing] = {}
        # Monotonic per-stream event sequence. Increments on every
        # broadcast; resets to 0 on ``stream_end`` so the next turn
        # starts fresh from 1. See :meth:`_stamp_sequence` for the FE
        # dedup contract.
        self._event_seq: int = 0
        self._closed = False
        self._connected = asyncio.Event()
        # Incoming frames from every client land here; ``receive()``
        # drains it. ``None`` is the close sentinel — enqueued only by
        # ``close()``, never on client disconnect.
        self._inbox: asyncio.Queue[Message | None] = asyncio.Queue()
        # Told ``(event, client_id)`` when a view attaches or detaches.
        self._connection_watchers: list[Callable[[str, str], None]] = []

    @property
    def port(self) -> int:
        """The bound port — meaningful after ``start()`` (supports port=0)."""
        return self._port

    @property
    def client_count(self) -> int:
        return len(self._conns)

    async def start(self) -> None:
        self._server = await serve(
            self._handler,
            self._host,
            self._port,
            max_size=_MAX_FRAME_BYTES,
            process_request=self._guard.process_request,
            select_subprotocol=self._guard.select_subprotocol,
        )
        # ``Server.sockets`` may be empty on platforms that don't expose
        # underlying sockets; the ``or []`` fallback keeps us typed.
        sockets = list(self._server.sockets or [])
        if sockets:
            self._port = sockets[0].getsockname()[1]
        logger.info(
            "BE listening on ws://%s:%d (%s)",
            self._host,
            self._port,
            "token required" if self._guard.requires_token else "no token; Origin checked",
        )

    @property
    def requires_token(self) -> bool:
        """Whether connections must offer the per-launch token."""
        return self._guard.requires_token

    @property
    def program_token(self) -> str | None:
        """The token for programs attaching through the lockfile."""
        return self._guard.program_token

    def mint_approver_token(self) -> str | None:
        """See :meth:`HandshakeGuard.mint_approver_token`."""
        return self._guard.mint_approver_token()

    async def revoke_approver_tokens(self) -> int:
        """Forget every approver token, and drop the programs connected with one.

        How many tokens there were. A dropped program can reconnect only
        as what the lockfile's token makes it: a program that approves
        nothing.
        """
        revoked = self._guard.revoke_approver_tokens()
        approvers = [
            client
            for client_id, client in list(self._conns.items())
            if self._standings.get(client_id) is Standing.APPROVER
        ]
        for client in approvers:
            try:
                await client.conn.close(code=1008, reason="approver token revoked")
            except Exception as exc:
                logger.info("approver %s did not close cleanly: %s", client.id, exc)
        return revoked

    async def wait_for_connection(self, timeout: float | None = 30.0) -> None:
        """Wait for the first client. ``timeout=None`` waits forever —
        GUI shells may open the webview long after spawning the BE."""
        await asyncio.wait_for(self._connected.wait(), timeout=timeout)

    def forget_past_connections(self) -> None:
        """Clients that came and went no longer count: the next is the first.

        :meth:`wait_for_connection` waits again and the event sequence
        starts over — for a connection made only to exercise the path
        (the cloud's warm base practises its first answer). Nothing
        changes while a client is attached.
        """
        if self._conns:
            return
        self._connected.clear()
        self._event_seq = 0

    #: The header a platform's own connection carries (``X-Igni-Gateway: control``).
    #: In the cloud the node agent adds it to the gateway's control streams and
    #: strips it from every other, so the connection's own request says it.
    CONTROL_HEADER = "X-Igni-Gateway"
    CONTROL_VALUE = "control"

    @classmethod
    def _is_control(cls, conn: ServerConnection) -> bool:
        """Whether this connection is the platform's own control stream.

        The marker is on the connection's handshake request, never on a
        message: a client cannot claim to be the platform. A connection
        with no request (never one that got this far) is not control.
        """
        request = conn.request
        if request is None:
            return False
        value = request.headers.get(cls.CONTROL_HEADER) or ""
        return value.strip().lower() == cls.CONTROL_VALUE

    def _standing_of(self, conn: ServerConnection) -> Standing:
        """What a connection may do, decided by the token it offered.

        The handshake admitted the connection, so the guard has an
        answer for it; a connection with no request at all (never one
        that got this far) is a program, which approves nothing.
        """
        request: Request | None = conn.request
        if request is None:
            return Standing.PROGRAM
        return self._guard.standing(request) or Standing.PROGRAM

    def watch_connections(self, watcher: Callable[[str, str], None]) -> None:
        """Be told ``("attached" | "detached", client_id)`` for every view.

        How the host object model knows a window went away: a window
        says who it is when it reads its state, and its connection
        closing is the only word it gets to say when it leaves.
        """
        self._connection_watchers.append(watcher)

    def _tell_watchers(self, event: str, client_id: str) -> None:
        for watcher in list(self._connection_watchers):
            try:
                watcher(event, client_id)
            except Exception:
                logger.exception("a connection watcher failed on %s %s", event, client_id)

    async def _handler(self, conn: ServerConnection) -> None:
        client_id = f"ws-{uuid.uuid4().hex[:8]}"
        client = MirroredClient(client_id=client_id, conn=conn)
        self._conns[client_id] = client
        self._connected.set()
        control = self._is_control(conn)
        # Decided by the token offered at the handshake, which let it in.
        standing = self._standing_of(conn)
        self._standings[client_id] = standing
        try:
            # Tell the client who it is so it can recognise its own
            # broadcast echoes. First — before logging it, too: whatever
            # watches attachments can wait until the client has heard
            # back (on a cloud VM just restored, every line of code run
            # before the welcome is memory it waits for).
            await conn.send(Welcome(client_id=client_id).model_dump_json())
            logger.info("FE client %s attached (%d total)", client_id, len(self._conns))
            self._tell_watchers("attached", client_id)
            async for raw in conn:
                if isinstance(raw, bytes):
                    # Bytes that are not UTF-8 read as garbage the registry
                    # refuses, like any other frame that is not a message.
                    raw = raw.decode(errors="replace")
                msg = self._registry.deserialize(raw)
                if msg is not None:
                    # Which connection said it: from the connection, never
                    # from the message (see ``Message.from_client``).
                    msg.mark_from(client_id, standing)
                    if control:
                        msg.mark_control()
                    await self._inbox.put(msg)
        except Exception as exc:
            logger.info("WS client %s ended: %s", client_id, exc)
        finally:
            self._conns.pop(client_id, None)
            self._standings.pop(client_id, None)
            logger.info("FE client %s detached (%d left)", client_id, len(self._conns))
            self._tell_watchers("detached", client_id)

    def owns_client(self, client_id: str) -> bool:
        """Whether ``client_id`` is a connection of this transport."""
        return client_id in self._conns

    async def _send_to(self, client_id: str, message: Message) -> None:
        """Send ``message`` to one connection only — a reply addressed to it.

        A reply that carries a secret (a page's service token) or one
        extension's data (its host-model answers, its calls' results)
        goes to the window that asked and nobody else; every other
        message is still broadcast. Not stamped with ``event_seq``, so
        no other client sees a gap in its sequence. A connection that
        has gone takes its reply with it.
        """
        client = self._conns.get(client_id)
        if client is None:
            logger.debug("reply to %s dropped: the connection has gone", client_id)
            return
        result = await client.send(message.model_dump_json(), timeout=self._broadcast_send_timeout)
        if not result.ok:
            logger.debug("WS send to %s failed (%s)", client_id, result.reason)
            self._conns.pop(client_id, None)

    def _stamp_sequence(self, message: Message) -> Message:
        """Stamp the message with the stream's next sequence number.

        A per-stream monotonic ``event_seq`` lets the FE dedup events
        that arrive twice (e.g. when two WebSocket clients are attached
        due to a StrictMode double-mount of IgniClient) AND preserves
        the ordering across duplicates — the FE keys dedup on
        (id, event_seq) and the sequence itself is the canonical order
        of events in the stream. A ``stream_end`` resets the counter so
        the next turn starts fresh from 1.
        """
        if not message.event_seq:
            self._event_seq += 1
            message = message.model_copy(update={"event_seq": self._event_seq})
        if isinstance(message, StreamEnd):
            self._event_seq = 0
        return message

    async def send(self, message: Message) -> None:
        """Broadcast to every attached client concurrently; drop dead /
        stalled connections. A message addressed to one connection
        (``Message.to_client``) goes to that connection only.

        Concurrency matters here: with multiple sessions live on one
        BE, a single wedged client (e.g. a webview the OS suspended
        mid-tab) used to stall *every* session's emit loop while we
        awaited its ``conn.send`` in series. Each conn now gets its
        own timeout via :meth:`MirroredClient.send` so a slow client
        only delays itself, and is evicted from ``_conns`` after the
        per-conn timeout.
        """
        if self._closed or not self._conns:
            # No views attached (e.g. all webviews mid-reload). Events
            # are fire-and-forget; RPC callers re-issue after reconnect.
            return
        if message.to_client:
            await self._send_to(message.to_client, message)
            return
        message = self._stamp_sequence(message)
        if isinstance(message, ContentDelta):
            # The operator's chunk trace: one line per chunk that
            # leaves, so a person can see whether the backend streams
            # every chunk or coalesces or drops them.
            self._chunk_trace.record(message)
        payload = message.model_dump_json()

        results: list[SendResult] = await asyncio.gather(
            *(
                client.send(payload, timeout=self._broadcast_send_timeout)
                for client in list(self._conns.values())
            )
        )
        for result in results:
            if result.ok:
                continue
            # Log at the level operators expect. Timeouts are worth a
            # WARNING (the client backed up its OS buffer or wedged);
            # peer-gone errors are DEBUG (the client just detached and
            # the driver noticed on our send).
            if result.reason == "timeout":
                logger.warning(
                    "WS send to %s timed out after %.1fs — dropping client",
                    result.client_id,
                    self._broadcast_send_timeout,
                )
            else:
                logger.debug("WS send to %s failed (client gone?)", result.client_id)
            self._conns.pop(result.client_id, None)

    async def receive(self) -> AsyncIterator[Message]:
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
        for client in list(self._conns.values()):
            await client.close()
        self._conns.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def is_closed(self) -> bool:
        return self._closed
