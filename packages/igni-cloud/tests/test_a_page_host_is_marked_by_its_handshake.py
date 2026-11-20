"""A page host is marked by its connection's handshake, never by a message.

``docs/design/local-cloud-switch.md`` §15.2. The node agent adds
``X-Igni-Gateway: pages`` to the stream of a person's desktop, as it adds
``control`` to the gateway's own, and strips the header from every
client's. igni reads it off the connection, trimmed and lowercased; any
other value is no mark, and nothing a message carries sets one.
"""

from __future__ import annotations

import json

import pytest
from websockets.asyncio.client import connect

from igni_core.protocol import messages as msg
from igni_core.transport.websocket import StreamMark, WebSocketServerTransport


async def _first_message(headers: dict[str, str] | None) -> msg.Message:
    transport = WebSocketServerTransport(port=0)
    await transport.start()
    try:
        async with connect(
            f"ws://127.0.0.1:{transport.port}", additional_headers=headers
        ) as window:
            await window.recv()  # welcome
            raw = json.loads(msg.RPCRequest(id="r1", method="get_status").model_dump_json())
            # A client claiming either mark is nothing.
            raw["_via_page_host"] = True
            raw["_via_control"] = True
            raw["via_page_host"] = True
            await window.send(json.dumps(raw))
            async for message in transport.receive():
                return message
    finally:
        await transport.close()
    raise AssertionError("nothing arrived")


async def test_the_pages_mark_makes_a_page_host_and_not_the_control_stream():
    message = await _first_message({"X-Igni-Gateway": "pages"})
    assert message.via_page_host and not message.via_control


async def test_the_control_mark_makes_the_control_stream_and_not_a_page_host():
    message = await _first_message({"X-Igni-Gateway": "control"})
    assert message.via_control and not message.via_page_host


@pytest.mark.parametrize(
    ("value", "page_host"),
    [("Pages", True), (" pages ", True), ("page", False), ("pages,control", False)],
)
async def test_the_mark_is_read_trimmed_and_lowercased_and_nothing_else_is_one(
    value: str, page_host: bool
):
    message = await _first_message({"X-Igni-Gateway": value})
    assert message.via_page_host is page_host
    assert not message.via_control


async def test_no_header_and_no_message_field_marks_anything():
    message = await _first_message(None)
    assert not message.via_page_host and not message.via_control


def test_the_marks_are_the_node_agent_s_words():
    assert StreamMark.CONTROL.value == WebSocketServerTransport.CONTROL_VALUE == "control"
    assert StreamMark.PAGES.value == WebSocketServerTransport.PAGE_HOST_VALUE == "pages"
    assert WebSocketServerTransport.CONTROL_HEADER == "X-Igni-Gateway"
