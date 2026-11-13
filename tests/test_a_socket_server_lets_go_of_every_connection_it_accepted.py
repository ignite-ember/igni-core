"""The socket server serves one frontend; every other connection is let go.

A connection it will not serve (an earlier one that a later one replaced,
one that arrived while the backend was shutting down) must be closed, or
the backend's own close waits for it forever.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from igni_core.protocol import messages as msg
from igni_core.transport.unix_socket import (
    UnixSocketClientTransport,
    UnixSocketServerTransport,
)


@pytest.fixture
def socket_path():
    directory = Path(tempfile.mkdtemp(prefix="igni-sock-"))
    try:
        yield directory / "be.sock"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


async def _first(transport) -> msg.Message:
    async for m in transport.receive():
        return m
    raise AssertionError("the stream ended")


async def test_a_later_connection_replaces_the_earlier_and_the_earlier_is_closed(
    socket_path: Path,
):
    server = UnixSocketServerTransport(socket_path)
    await server.start()
    try:
        first = UnixSocketClientTransport(socket_path)
        await first.connect(timeout=5)
        await server.wait_for_connection(timeout=5)
        second = UnixSocketClientTransport(socket_path)
        await second.connect(timeout=5)
        await asyncio.sleep(0.1)

        # The earlier frontend is told the connection is over...
        async def drain():
            return [m async for m in first.receive()]

        assert await asyncio.wait_for(drain(), 5) == []
        # ...and only the later one is served.
        await server.send(msg.Info(text="who hears"))
        got = await asyncio.wait_for(_first(second), 5)
        assert isinstance(got, msg.Info) and got.text == "who hears"
        await first.close()
        await second.close()
    finally:
        await asyncio.wait_for(server.close(), 5)


async def test_closing_right_after_a_client_connects_does_not_wait_for_it(socket_path: Path):
    server = UnixSocketServerTransport(socket_path)
    await server.start()
    client = UnixSocketClientTransport(socket_path)
    await client.connect(timeout=5)
    # No wait for the server to notice: the frontend leaves and the
    # backend shuts down at once.
    await client.close()
    await asyncio.wait_for(server.close(), 5)
    assert not socket_path.exists()
