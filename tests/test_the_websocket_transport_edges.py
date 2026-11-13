"""The WebSocket transport's edges: frames, addressing, watchers, control, eviction.

What a browser tab or a program on the loopback port sees of the
backend: frames of any kind that are not messages do not end the
connection, a reply addressed to one connection reaches that one only,
the connection watchers hear attach and detach, the platform's control
header is read from the handshake alone, and a client that cannot be
sent to is dropped.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from websockets.asyncio.client import connect

from igni_core.protocol import messages as msg
from igni_core.transport.websocket import WebSocketServerTransport

_WS_LOG = "igni_core.transport.websocket"


@pytest.fixture
async def transport():
    tr = WebSocketServerTransport(port=0, broadcast_send_timeout=0.2)
    await tr.start()
    try:
        yield tr
    finally:
        await tr.close()


async def _attach(tr: WebSocketServerTransport, **kw):
    ws = await connect(f"ws://127.0.0.1:{tr.port}", **kw)
    welcome = json.loads(await asyncio.wait_for(ws.recv(), 5))
    assert welcome["type"] == "welcome"
    return ws, welcome["client_id"]


async def _next_inbound(tr: WebSocketServerTransport) -> msg.Message:
    async def first() -> msg.Message:
        async for m in tr.receive():
            return m
        raise AssertionError("the stream ended")

    return await asyncio.wait_for(first(), 5)


async def _until(predicate, seconds: float = 5.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), seconds)


# ── inbound frames ──────────────────────────────────────────────


async def test_a_binary_frame_is_read_as_text_like_any_other(transport):
    ws, client_id = await _attach(transport)
    await ws.send(msg.UserMessage(text="in bytes", id="b1").model_dump_json().encode())
    got = await _next_inbound(transport)
    assert isinstance(got, msg.UserMessage) and got.text == "in bytes"
    assert got.from_client == client_id
    await ws.close()


async def test_frames_that_are_not_messages_are_dropped_and_the_connection_goes_on(transport):
    ws, client_id = await _attach(transport)
    for noise in (
        "not json",
        "[1, 2]",
        json.dumps({"type": "no-such-message"}),
    ):
        await ws.send(noise)
    await ws.send(msg.UserMessage(text="after the noise", id="n1").model_dump_json())

    got = await _next_inbound(transport)

    assert got.text == "after the noise"
    assert transport.owns_client(client_id)
    await ws.close()


async def test_who_said_it_comes_from_the_connection_never_from_the_message(transport):
    ws, client_id = await _attach(transport)
    forged = json.loads(msg.UserMessage(text="forged", id="f1").model_dump_json())
    forged.update({"client_id": "ws-someone-else", "from_client": "ws-someone-else"})
    await ws.send(json.dumps(forged))
    got = await _next_inbound(transport)
    assert got.from_client == client_id
    await ws.close()


async def test_a_large_message_far_past_the_library_default_arrives_whole(transport):
    ws, _ = await _attach(transport)
    big = "x" * (20 * 1024 * 1024)
    await ws.send(msg.UserMessage(text=big, id="big").model_dump_json())
    got = await _next_inbound(transport)
    assert got.text == big
    await ws.close()


async def test_a_frame_over_the_64_mib_cap_ends_that_connection_only(transport):
    big, big_id = await _attach(transport)
    other, other_id = await _attach(transport)
    await big.send("x" * (64 * 1024 * 1024 + 1))
    await asyncio.wait_for(big.wait_closed(), 20)
    assert big.close_code == 1009
    await _until(lambda: not transport.owns_client(big_id))
    assert transport.owns_client(other_id)
    await other.close()


# ── the control stream ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "control"),
    [
        ("control", True),
        ("CONTROL", True),
        ("  Control  ", True),
        ("controller", False),
        ("", False),
    ],
)
async def test_only_the_gateway_header_marks_a_connection_as_control(transport, value, control):
    ws, _ = await _attach(transport, additional_headers={"X-Igni-Gateway": value})
    await ws.send(msg.UserMessage(text="hi", id="c1").model_dump_json())
    got = await _next_inbound(transport)
    assert got.via_control is control
    await ws.close()


async def test_a_message_cannot_claim_to_be_the_platform(transport):
    ws, _ = await _attach(transport)
    await ws.send(msg.UserMessage(text="hi", id="c2").model_dump_json())
    got = await _next_inbound(transport)
    assert got.via_control is False
    await ws.close()


# ── addressed replies ───────────────────────────────────────────


async def test_an_addressed_reply_reaches_that_connection_only_and_is_not_stamped(transport):
    ws_a, id_a = await _attach(transport)
    ws_b, _id_b = await _attach(transport)

    await transport.send(msg.Info(text="for a").address_to(id_a))
    await transport.send(msg.Info(text="for everyone"))

    first_a = json.loads(await asyncio.wait_for(ws_a.recv(), 5))
    first_b = json.loads(await asyncio.wait_for(ws_b.recv(), 5))
    assert first_a["text"] == "for a"
    assert first_a["event_seq"] == 0
    # B's first frame is the broadcast, and its sequence has no gap.
    assert first_b["text"] == "for everyone"
    assert first_b["event_seq"] == 1
    await ws_a.close()
    await ws_b.close()


async def test_an_addressed_reply_to_a_connection_that_has_gone_is_dropped_silently(
    transport, caplog
):
    ws, client_id = await _attach(transport)
    keeper, _ = await _attach(transport)
    await ws.close()
    await _until(lambda: not transport.owns_client(client_id))

    with caplog.at_level(logging.DEBUG, logger=_WS_LOG):
        await transport.send(msg.Info(text="too late").address_to(client_id))

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert transport.client_count == 1
    await keeper.close()


async def test_an_addressed_reply_that_cannot_be_sent_evicts_that_connection(transport):
    ws, client_id = await _attach(transport)
    keeper, keeper_id = await _attach(transport)

    async def broken(_payload):
        raise ConnectionResetError("gone")

    transport._conns[client_id].conn.send = broken
    await transport.send(msg.Info(text="doomed").address_to(client_id))

    assert not transport.owns_client(client_id)
    assert transport.owns_client(keeper_id)
    await ws.close()
    await keeper.close()


async def test_owning_a_client_is_having_its_connection(transport):
    ws, client_id = await _attach(transport)
    assert transport.owns_client(client_id)
    assert not transport.owns_client("ws-nobody")
    assert not transport.owns_client("")
    await ws.close()
    await _until(lambda: not transport.owns_client(client_id))
    assert transport.client_count == 0


# ── eviction and broadcasting ───────────────────────────────────


async def test_a_client_whose_send_fails_is_evicted_without_a_warning(transport, caplog):
    ws, client_id = await _attach(transport)
    other, other_id = await _attach(transport)

    async def broken(_payload):
        raise ConnectionResetError("gone")

    transport._conns[client_id].conn.send = broken
    with caplog.at_level(logging.DEBUG, logger=_WS_LOG):
        await transport.send(msg.Info(text="hello"))

    assert not transport.owns_client(client_id)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert json.loads(await asyncio.wait_for(other.recv(), 5))["text"] == "hello"
    assert transport.owns_client(other_id)
    await ws.close()
    await other.close()


async def test_a_client_that_times_out_is_evicted_with_a_warning(transport, caplog):
    ws, client_id = await _attach(transport)

    async def parked(_payload):
        await asyncio.Event().wait()

    transport._conns[client_id].conn.send = parked
    with caplog.at_level(logging.WARNING, logger=_WS_LOG):
        await transport.send(msg.Info(text="hello"))

    assert not transport.owns_client(client_id)
    assert any(
        "timed out" in r.getMessage() and client_id in r.getMessage() for r in caplog.records
    )
    await ws.close()


async def test_a_message_that_already_has_a_sequence_keeps_it(transport):
    ws, _ = await _attach(transport)
    await transport.send(msg.Info(text="preset", event_seq=41))
    await transport.send(msg.Info(text="stamped"))
    assert json.loads(await asyncio.wait_for(ws.recv(), 5))["event_seq"] == 41
    assert json.loads(await asyncio.wait_for(ws.recv(), 5))["event_seq"] == 1
    await ws.close()


async def test_sending_after_close_sends_nothing_and_close_is_idempotent():
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    ws, _ = await _attach(tr)
    await tr.close()
    await tr.close()
    assert tr.is_closed
    await tr.send(msg.Info(text="after close"))
    with pytest.raises(Exception):  # noqa: B017 — the connection is closed: ConnectionClosed
        await asyncio.wait_for(ws.recv(), 5)
    assert tr.client_count == 0


async def test_close_ends_every_connection_and_the_receive():
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    ws, _ = await _attach(tr)
    reader = asyncio.ensure_future(_drain(tr))
    await asyncio.sleep(0.02)
    await tr.close()
    assert await asyncio.wait_for(reader, 5) == []
    await asyncio.wait_for(ws.wait_closed(), 5)


async def _drain(tr: WebSocketServerTransport) -> list[msg.Message]:
    return [m async for m in tr.receive()]


async def test_the_port_cannot_be_taken_twice(transport):
    second = WebSocketServerTransport(port=transport.port)
    with pytest.raises(OSError):
        await second.start()
    await second.close()


async def test_waiting_for_a_first_client_times_out():
    tr = WebSocketServerTransport(port=0)
    await tr.start()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await tr.wait_for_connection(timeout=0.05)
    finally:
        await tr.close()


# ── forgetting past connections ─────────────────────────────────


async def test_forgetting_changes_nothing_while_a_client_is_attached(transport):
    ws, _ = await _attach(transport)
    await transport.send(msg.Info(text="one"))
    transport.forget_past_connections()
    await asyncio.wait_for(transport.wait_for_connection(timeout=None), 1)
    await transport.send(msg.Info(text="two"))
    await asyncio.wait_for(ws.recv(), 5)  # one
    assert json.loads(await asyncio.wait_for(ws.recv(), 5))["event_seq"] == 2
    await ws.close()


async def test_forgetting_with_no_client_makes_the_next_the_first_and_restarts_the_sequence(
    transport,
):
    ws, _ = await _attach(transport)
    await transport.send(msg.Info(text="one"))
    await transport.send(msg.Info(text="two"))
    await ws.close()
    await _until(lambda: transport.client_count == 0)

    transport.forget_past_connections()

    with pytest.raises(asyncio.TimeoutError):
        await transport.wait_for_connection(timeout=0.05)
    again, _ = await _attach(transport)
    await asyncio.wait_for(transport.wait_for_connection(timeout=5), 5)
    await transport.send(msg.Info(text="fresh"))
    assert json.loads(await asyncio.wait_for(again.recv(), 5))["event_seq"] == 1
    await again.close()


# ── watchers ────────────────────────────────────────────────────


async def test_watchers_hear_attach_after_the_welcome_and_detach_on_disconnect(transport):
    told: list[tuple[str, str]] = []
    transport.watch_connections(lambda event, client_id: told.append((event, client_id)))

    ws, client_id = await _attach(transport)
    await _until(lambda: told == [("attached", client_id)])
    await ws.close()
    await _until(lambda: told == [("attached", client_id), ("detached", client_id)])


async def test_a_watcher_that_raises_stops_neither_the_others_nor_the_detach(transport, caplog):
    heard: list[tuple[str, str]] = []

    def broken(event: str, client_id: str) -> None:
        raise RuntimeError("watcher bug")

    transport.watch_connections(broken)
    transport.watch_connections(lambda event, client_id: heard.append((event, client_id)))

    with caplog.at_level(logging.ERROR, logger=_WS_LOG):
        ws, client_id = await _attach(transport)
        await _until(lambda: heard == [("attached", client_id)])
        await ws.close()
        await _until(lambda: len(heard) == 2)

    assert heard[1] == ("detached", client_id)
    assert "watcher bug" in caplog.text
    assert transport.client_count == 0


# ── revoking approvers ──────────────────────────────────────────


async def test_revoking_with_no_approver_changes_nothing():
    tr = WebSocketServerTransport(port=0, token="launch-secret-0123456789abcdefghijklmnop")
    await tr.start()
    try:
        assert await tr.revoke_approver_tokens() == 0
    finally:
        await tr.close()


async def test_a_connection_that_will_not_close_when_its_token_is_revoked_is_logged_not_raised(
    caplog,
):
    secret = "launch-secret-0123456789abcdefghijklmnop"
    tr = WebSocketServerTransport(port=0, token=secret)
    await tr.start()
    try:
        approver_token = tr.mint_approver_token()
        assert approver_token
        ws = await connect(
            f"ws://127.0.0.1:{tr.port}", subprotocols=["igni.v1", f"igni-token.{approver_token}"]
        )
        await asyncio.wait_for(ws.recv(), 5)
        (client,) = tr._conns.values()

        async def refuse(code=1000, reason=""):
            raise RuntimeError("will not close")

        real_close = client.conn.close
        client.conn.close = refuse
        try:
            with caplog.at_level(logging.INFO, logger=_WS_LOG):
                assert await asyncio.wait_for(tr.revoke_approver_tokens(), 5) == 1
        finally:
            client.conn.close = real_close
        assert "will not close" in caplog.text
        await ws.close()
    finally:
        await asyncio.wait_for(tr.close(), 5)
