"""A binary frame whose bytes are not UTF-8 is a bad frame, not the end of the view.

Frames that are not messages are dropped and the connection goes on; a
frame of bytes that cannot be read as text is the same kind of frame.
"""

from __future__ import annotations

import asyncio
import json

from websockets.asyncio.client import connect

from igni_core.protocol import messages as msg
from igni_core.transport.websocket import WebSocketServerTransport


async def test_a_binary_frame_that_is_not_utf8_is_dropped_and_the_connection_goes_on():
    transport = WebSocketServerTransport(port=0)
    await transport.start()
    try:
        ws = await connect(f"ws://127.0.0.1:{transport.port}")
        client_id = json.loads(await asyncio.wait_for(ws.recv(), 5))["client_id"]
        await ws.send(b"\xff\xfe\x00 not utf-8")
        await ws.send(msg.UserMessage(text="after the noise", id="n1").model_dump_json())

        async def first() -> msg.Message:
            async for m in transport.receive():
                return m
            raise AssertionError("the stream ended")

        got = await asyncio.wait_for(first(), 5)

        assert got.text == "after the noise"
        assert transport.owns_client(client_id)
        await ws.close()
    finally:
        await transport.close()
