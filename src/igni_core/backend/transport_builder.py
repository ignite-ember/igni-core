"""The transport the backend's CLI flags ask for, and the ready line that names it."""

from __future__ import annotations

from igni_core.backend.schemas_rpc import BackendReadyLine
from igni_core.transport.base import ListeningTransport
from igni_core.transport.composite import CompositeTransport
from igni_core.transport.unix_socket import UnixSocketServerTransport
from igni_core.transport.websocket import WebSocketServerTransport


class TransportBuilder:
    """Pick and build the right transport for the CLI flags.

    Both flags together = mirrored session: the TUI attaches over
    the Unix socket while GUI tabs attach over WS, and every view
    receives every broadcast (via the composite transport's fan-out).
    """

    def __init__(
        self, *, socket_path: str | None, ws_port: int | None, ws_token: str | None = None
    ) -> None:
        self._socket_path = socket_path
        self._ws_port = ws_port
        # The launcher's per-launch secret; every WS connection must
        # offer it (``transport/ws_auth.py``). ``None``: Origin check only
        # (the cloud, or the development opt-out).
        self._ws_token = ws_token
        self._ws_transport: WebSocketServerTransport | None = None

    @property
    def program_token(self) -> str | None:
        """The token a program attaching to this backend offers, published
        in the owner-only lockfile. Never the launch secret, which is the
        window's alone (``transport/ws_auth.py``)."""
        if self._ws_transport is None:
            return None
        return self._ws_transport.program_token

    @property
    def ws_transport(self) -> WebSocketServerTransport | None:
        """The WS transport, once built, for its bound port."""
        return self._ws_transport

    async def build(self) -> ListeningTransport:
        """Build the transport the flags ask for.

        One flag: that transport. Both: the composite of the two, so
        every view mirrors one session.
        """
        children: list[ListeningTransport] = []
        if self._socket_path is not None:
            children.append(UnixSocketServerTransport(self._socket_path))
        if self._ws_port is not None:
            self._ws_transport = WebSocketServerTransport(port=self._ws_port, token=self._ws_token)
            children.append(self._ws_transport)

        transport: ListeningTransport = (
            children[0] if len(children) == 1 else CompositeTransport(list(children))
        )
        await transport.start()
        return transport

    def ready_line(self) -> BackendReadyLine:
        """The "ready, connect here" line for what was built."""
        ws = self._ws_transport
        return BackendReadyLine(
            status="ready",
            ws_port=ws.port if ws is not None else None,
            ws_url=f"ws://127.0.0.1:{ws.port}" if ws is not None else None,
            socket=str(self._socket_path) if self._socket_path is not None else None,
        )
