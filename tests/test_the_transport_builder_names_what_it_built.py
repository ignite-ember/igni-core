"""The transport the CLI flags ask for, and the ready line that names it.

``TransportBuilder`` builds real transports on free ports and a real Unix
socket; the ready line is what the window's launcher parses to find the backend.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

from igni_core.backend.transport_builder import TransportBuilder
from igni_core.transport.unix_socket import UnixSocketServerTransport
from igni_core.transport.websocket import WebSocketServerTransport

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets")


@pytest.fixture
def socket_path():
    with tempfile.TemporaryDirectory(prefix="igni-tb-", dir="/tmp") as d:
        yield Path(d) / "be.sock"


@POSIX_ONLY
async def test_a_socket_path_alone_builds_a_unix_transport_and_a_ready_line_with_only_the_socket(
    socket_path,
) -> None:
    builder = TransportBuilder(socket_path=str(socket_path), ws_port=None)
    transport = await builder.build()
    try:
        assert isinstance(transport, UnixSocketServerTransport)
        assert socket_path.exists()
        assert builder.ws_transport is None
        assert builder.program_token is None
        line = builder.ready_line()
        assert line.status == "ready"
        assert line.socket == str(socket_path)
        assert line.ws_port is None
        assert line.ws_url is None
    finally:
        await transport.close()


async def test_the_ready_line_names_the_bound_port_when_the_flag_asked_for_port_zero() -> None:
    builder = TransportBuilder(socket_path=None, ws_port=0, ws_token="launch-secret")
    transport = await builder.build()
    try:
        assert isinstance(transport, WebSocketServerTransport)
        line = builder.ready_line()
        assert line.ws_port == transport.port > 0
        assert line.ws_url == f"ws://127.0.0.1:{transport.port}"
        assert line.socket is None
    finally:
        await transport.close()


@POSIX_ONLY
async def test_a_path_object_is_named_as_a_string_and_both_flags_fill_every_field(
    socket_path,
) -> None:
    builder = TransportBuilder(socket_path=socket_path, ws_port=0)  # type: ignore[arg-type]
    transport = await builder.build()
    try:
        line = builder.ready_line()
        assert line.socket == str(socket_path)
        assert line.ws_port == builder.ws_transport.port  # type: ignore[union-attr]
        assert line.ws_url == f"ws://127.0.0.1:{line.ws_port}"
    finally:
        await transport.close()


async def test_the_program_token_is_published_and_is_never_the_launch_secret() -> None:
    builder = TransportBuilder(socket_path=None, ws_port=0, ws_token="launch-secret")
    assert builder.program_token is None  # nothing built yet
    transport = await builder.build()
    try:
        token = builder.program_token
        assert token
        assert token != "launch-secret"
        assert token == builder.ws_transport.program_token  # type: ignore[union-attr]
    finally:
        await transport.close()


async def test_a_port_already_in_use_is_an_error_not_a_silent_second_listener() -> None:
    first = TransportBuilder(socket_path=None, ws_port=0)
    transport = await first.build()
    try:
        port = first.ws_transport.port  # type: ignore[union-attr]
        with pytest.raises(OSError):
            await TransportBuilder(socket_path=None, ws_port=port).build()
    finally:
        await transport.close()
