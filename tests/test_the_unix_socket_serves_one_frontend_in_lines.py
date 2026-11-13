"""The Unix socket transport: one frontend, one JSON message per line.

What the terminal sees of the backend's socket: a stale file at the path
is replaced, the parent directory is made, one client is served at a
time, a line that is not a message is skipped and the next one still
arrives, and a message far past asyncio's 64 KiB line default still gets
through.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from igni_core.protocol import messages as msg
from igni_core.transport.base import ClientAwareTransport
from igni_core.transport.ndjson_stream import STREAM_LIMIT
from igni_core.transport.unix_socket import (
    UnixSocketClientTransport,
    UnixSocketServerTransport,
)


@pytest.fixture
def socket_path() -> Path:
    # AF_UNIX paths are short; pytest's tmp_path can be too long on macOS.
    import tempfile

    directory = Path(tempfile.mkdtemp(prefix="igni-sock-"))
    try:
        yield directory / "be.sock"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


async def _raw_client(path: Path):
    return await asyncio.open_unix_connection(str(path))


async def _next(transport, timeout: float = 5.0) -> msg.Message:
    async def first() -> msg.Message:
        async for m in transport.receive():
            return m
        raise AssertionError("the stream ended")

    return await asyncio.wait_for(first(), timeout)


async def test_start_makes_the_directory_and_replaces_a_stale_socket_file(socket_path: Path):
    path = socket_path.parent / "a" / "b" / "be.sock"
    server = UnixSocketServerTransport(path)
    await server.start()
    await server.close()
    assert not path.exists()

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("left behind by a crashed backend")
    server = UnixSocketServerTransport(str(path))
    try:
        await server.start()
        client = UnixSocketClientTransport(path)
        await client.connect(timeout=5)
        await client.close()
    finally:
        await server.close()
        path.parent.rmdir()
        path.parent.parent.rmdir()


async def test_close_removes_the_socket_file_and_is_idempotent(socket_path: Path):
    server = UnixSocketServerTransport(socket_path)
    await server.start()
    assert socket_path.exists()
    await server.close()
    assert server.is_closed
    assert not socket_path.exists()
    await server.close()


async def test_close_before_start_is_harmless(socket_path: Path):
    server = UnixSocketServerTransport(socket_path)
    await server.close()
    assert server.is_closed


async def test_waiting_for_a_client_that_never_comes_times_out(socket_path: Path):
    server = UnixSocketServerTransport(socket_path)
    await server.start()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await server.wait_for_connection(timeout=0.05)
    finally:
        await server.close()


async def test_the_server_counts_its_one_client_and_owns_no_named_client(socket_path: Path):
    server = UnixSocketServerTransport(socket_path)
    assert isinstance(server, ClientAwareTransport)
    await server.start()
    try:
        assert server.client_count == 0
        client = UnixSocketClientTransport(socket_path)
        await client.connect(timeout=5)
        await server.wait_for_connection(timeout=5)
        assert server.client_count == 1
        assert server.owns_client("ws-anything") is False
        assert server.owns_client("") is False

        # The count is of the server's own writer: it closes with the server.
        await server.close()
        assert server.client_count == 0
        await client.close()
    finally:
        await server.close()


async def test_a_client_of_a_socket_that_is_not_there_fails_clearly(socket_path: Path):
    client = UnixSocketClientTransport(socket_path)
    with pytest.raises(OSError):
        await client.connect(timeout=1)


async def test_a_client_that_has_not_connected_cannot_receive(socket_path: Path):
    client = UnixSocketClientTransport(socket_path)
    with pytest.raises(RuntimeError, match="Not connected"):
        await client.receive().__anext__()


async def test_the_server_waits_in_receive_for_a_client_that_attaches_later(socket_path: Path):
    server = UnixSocketServerTransport(socket_path)
    await server.start()
    try:
        waiting = asyncio.ensure_future(_next(server))
        await asyncio.sleep(0.05)
        assert not waiting.done()
        client = UnixSocketClientTransport(socket_path)
        await client.connect(timeout=5)
        await client.send(msg.UserMessage(text="late", id="u1"))
        got = await asyncio.wait_for(waiting, 5)
        assert isinstance(got, msg.UserMessage) and got.text == "late"
        await client.close()
    finally:
        await server.close()


async def test_send_without_a_connection_or_after_close_is_dropped(socket_path: Path):
    server = UnixSocketServerTransport(socket_path)
    await server.send(msg.Info(text="nobody there"))
    await server.start()
    client = UnixSocketClientTransport(socket_path)
    await client.connect(timeout=5)
    await server.wait_for_connection(timeout=5)
    await server.close()
    await server.send(msg.Info(text="after close"))
    # The client sees the connection end and nothing else.
    assert [m async for m in client.receive()] == []
    await client.close()
    await client.send(msg.Info(text="after close"))
    await client.close()


async def test_blank_unknown_and_malformed_lines_are_skipped(socket_path: Path):
    server = UnixSocketServerTransport(socket_path)
    await server.start()
    try:
        _reader, writer = await _raw_client(socket_path)
        user = msg.UserMessage(text="after the noise", id="u2").model_dump_json()
        writer.write(
            b"\n   \n"
            b"not json at all\n"
            b'{"type": "no-such-message-type"}\n'
            b"[1, 2]\n" + user.encode() + b"\n"
        )
        await writer.drain()
        got = await _next(server)
        assert isinstance(got, msg.UserMessage) and got.text == "after the noise"
        writer.close()
    finally:
        await server.close()


async def test_the_line_limit_is_far_past_asyncio_s_default():
    assert STREAM_LIMIT == 64 * 1024 * 1024


async def test_a_message_past_64_kib_arrives_whole(socket_path: Path):
    server = UnixSocketServerTransport(socket_path)
    await server.start()
    try:
        client = UnixSocketClientTransport(socket_path)
        await client.connect(timeout=5)
        big = "é" * (3 * 1024 * 1024)
        # The send's drain finishes only as the other side reads.
        sending = asyncio.ensure_future(client.send(msg.UserMessage(text=big, id="big")))
        got = await _next(server, timeout=5)
        await asyncio.wait_for(sending, 5)
        assert isinstance(got, msg.UserMessage) and got.text == big
        sending = asyncio.ensure_future(server.send(msg.ContentDelta(text=big)))
        back = await _next(client, timeout=5)
        await asyncio.wait_for(sending, 5)
        assert isinstance(back, msg.ContentDelta) and back.text == big
        await client.close()
    finally:
        await asyncio.wait_for(server.close(), 5)


async def test_the_stream_ends_when_the_peer_goes(socket_path: Path):
    server = UnixSocketServerTransport(socket_path)
    await server.start()
    try:
        client = UnixSocketClientTransport(socket_path)
        await client.connect(timeout=5)
        await server.wait_for_connection(timeout=5)
        await client.send(msg.UserMessage(text="last", id="l"))
        await client.close()
        got = [m async for m in server.receive()]
        assert [m.text for m in got] == ["last"]
    finally:
        await server.close()


async def test_a_transport_hands_the_registry_it_was_given_to_its_lines(socket_path: Path):
    from igni_core.protocol.registry import MessageRegistry

    server = UnixSocketServerTransport(socket_path, registry=MessageRegistry([msg.Info]))
    await server.start()
    try:
        _r, writer = await _raw_client(socket_path)
        writer.write(
            msg.UserMessage(text="refused", id="x").model_dump_json().encode()
            + b"\n"
            + msg.Info(text="admitted").model_dump_json().encode()
            + b"\n"
        )
        await writer.drain()
        got = await _next(server)
        assert isinstance(got, msg.Info)
        writer.close()
    finally:
        await server.close()
