"""A line that is not text, or a peer that has gone, must not end the stream.

The registry skips a frame it cannot read so that "a bad message never
wedges the read loop"; a line of bytes that are not UTF-8 is the same
kind of bad frame. And a peer that went away is a message with nowhere to
go, as a send with no peer already is.
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


async def test_a_line_that_is_not_utf8_is_skipped_and_the_next_message_arrives(
    socket_path: Path,
):
    server = UnixSocketServerTransport(socket_path)
    await server.start()
    try:
        _reader, writer = await asyncio.open_unix_connection(str(socket_path))
        user = msg.UserMessage(text="still here", id="u3").model_dump_json()
        writer.write(b'\xff\xfe{"type": "user_message"}\n' + user.encode() + b"\n")
        await writer.drain()
        got = await asyncio.wait_for(_first(server), 5)
        assert isinstance(got, msg.UserMessage) and got.text == "still here"
        writer.close()
    finally:
        await asyncio.wait_for(server.close(), 5)


async def test_a_send_to_a_peer_that_has_gone_is_dropped_and_the_close_still_closes(socket_path: Path):
    server = UnixSocketServerTransport(socket_path)
    await server.start()
    try:
        client = UnixSocketClientTransport(socket_path)
        await client.connect(timeout=5)
        await server.wait_for_connection(timeout=5)
        await client.close()
        # The peer's close is noticed by the first writes after it; none
        # of them may raise.
        for _ in range(20):
            await server.send(msg.Info(text="anyone?"))
            await asyncio.sleep(0.01)
        assert not server.is_closed
    finally:
        await asyncio.wait_for(server.close(), 5)
