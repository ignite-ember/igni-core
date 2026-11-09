"""The chunk trace records every chunk the WebSocket transport broadcasts.

The trace is an operator's aid: with it on, every chunk that leaves the
backend leaves a line too, so a person can tell whether the backend
really streams every chunk or coalesces or drops them.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from websockets.asyncio.client import connect

from igni_core.protocol import messages as msg
from igni_core.transport.chunk_trace import ChunkTrace
from igni_core.transport.websocket import WebSocketServerTransport


async def _attached_client(port: int):
    """A client that has been welcomed; its welcome frame is consumed."""
    client = await connect(f"ws://127.0.0.1:{port}")
    await asyncio.wait_for(client.recv(), 5)
    return client


@pytest.mark.asyncio
async def test_a_broadcast_chunk_leaves_a_line_in_the_trace(tmp_path):
    trace_file = tmp_path / "chunk_trace.log"
    transport = WebSocketServerTransport(port=0, chunk_trace=ChunkTrace(path=trace_file))
    await transport.start()
    try:
        client = await _attached_client(transport.port)
        await transport.send(msg.ContentDelta(text="hello world"))
        await client.close()
    finally:
        await transport.close()

    lines = trace_file.read_text().splitlines()
    assert len(lines) == 1, f"one chunk, one line; got {lines}"
    assert "seq=1" in lines[0]
    assert "len=11" in lines[0]
    assert "thinking=False" in lines[0]
    assert "preview='hello world'" in lines[0]


@pytest.mark.asyncio
async def test_every_chunk_of_a_stream_leaves_its_own_line(tmp_path):
    trace_file = tmp_path / "chunk_trace.log"
    transport = WebSocketServerTransport(port=0, chunk_trace=ChunkTrace(path=trace_file))
    await transport.start()
    try:
        client = await _attached_client(transport.port)
        await transport.send(msg.ContentDelta(text="one"))
        await transport.send(msg.ContentDelta(text="two", is_thinking=True))
        await client.close()
    finally:
        await transport.close()

    lines = trace_file.read_text().splitlines()
    assert len(lines) == 2, f"two chunks, two lines; got {lines}"
    assert "seq=1" in lines[0] and "preview='one'" in lines[0]
    assert "seq=2" in lines[1] and "thinking=True" in lines[1]


@pytest.mark.asyncio
async def test_a_message_that_is_not_a_chunk_leaves_no_line(tmp_path):
    trace_file = tmp_path / "chunk_trace.log"
    transport = WebSocketServerTransport(port=0, chunk_trace=ChunkTrace(path=trace_file))
    await transport.start()
    try:
        client = await _attached_client(transport.port)
        await transport.send(msg.Info(text="not a chunk"))
        await client.close()
    finally:
        await transport.close()

    assert not trace_file.exists(), "only chunks are traced"


@pytest.mark.asyncio
async def test_the_trace_lands_even_when_the_process_silences_logging(tmp_path):
    """The trace writes its own file, so a process that turns logging
    off after startup still leaves a trace behind."""
    trace_file = tmp_path / "chunk_trace.log"
    transport = WebSocketServerTransport(port=0, chunk_trace=ChunkTrace(path=trace_file))
    await transport.start()
    try:
        client = await _attached_client(transport.port)
        logging.disable(logging.DEBUG)
        try:
            await transport.send(msg.ContentDelta(text="still traced"))
        finally:
            logging.disable(logging.NOTSET)
        await client.close()
    finally:
        await transport.close()

    assert "still traced" in trace_file.read_text()


def test_the_trace_is_off_unless_the_environment_asks_for_it(monkeypatch):
    monkeypatch.delenv("IGNI_CHUNK_TRACE", raising=False)
    assert not ChunkTrace.from_environment().enabled

    monkeypatch.setenv("IGNI_CHUNK_TRACE", "1")
    assert ChunkTrace.from_environment().enabled


def test_the_environment_says_where_the_trace_file_is(monkeypatch, tmp_path):
    trace_file = tmp_path / "somewhere" / "trace.log"
    monkeypatch.setenv("IGNI_CHUNK_TRACE", "1")
    monkeypatch.setenv("IGNI_CHUNK_TRACE_LOG", str(trace_file))

    ChunkTrace.from_environment().record(msg.ContentDelta(text="where", event_seq=7))

    assert "seq=7" in trace_file.read_text()


def test_a_disabled_trace_writes_nothing(tmp_path):
    trace_file = tmp_path / "chunk_trace.log"
    ChunkTrace(path=trace_file, enabled=False).record(msg.ContentDelta(text="nothing"))

    assert not trace_file.exists()
