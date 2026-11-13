"""The tapped read stream: registered methods go to their handler, the rest pass through.

A real :class:`McpNotificationRouter` and :class:`NotificationTapStream`
over a real in-memory stream carrying the SDK's own messages. The
end-to-end path through a live extension process is in
``test_an_extension_notifies_the_person_at_its_machine.py``.
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCNotification, JSONRPCRequest, JSONRPCResponse

from igni_core.core.mcp.notification_tap import McpNotificationRouter

pytestmark = pytest.mark.asyncio

METHOD = "notifications/igni/notify"


def _notification(method: str, params: Any = None) -> SessionMessage:
    return SessionMessage(JSONRPCNotification(jsonrpc="2.0", method=method, params=params))


def _stream(*messages: Any):
    send, receive = anyio.create_memory_object_stream[Any](len(messages) + 1)
    for message in messages:
        send.send_nowait(message)
    return send, receive


async def test_a_registered_notification_goes_to_its_handler_and_not_to_the_sdk():
    router = McpNotificationRouter()
    seen: list[tuple[str, dict[str, Any]]] = []
    router.handle(METHOD, lambda server, params: seen.append((server, params)))
    other = _notification("notifications/progress", {"progress": 1})
    _, receive = _stream(_notification(METHOD, {"title": "Ready"}), other)

    tapped = router.tap("acme", receive)

    assert await tapped.receive() is other
    assert seen == [("acme", {"title": "Ready"})]


async def test_the_sender_is_the_connection_not_a_name_the_payload_claims():
    router = McpNotificationRouter()
    seen: list[str] = []
    router.handle(METHOD, lambda server, params: seen.append(server))
    send, receive = _stream(_notification(METHOD, {"server": "other", "extension": "other"}))
    send.close()

    with pytest.raises(anyio.EndOfStream):
        await router.tap("acme", receive).receive()
    assert seen == ["acme"]


async def test_params_that_are_not_an_object_reach_the_handler_as_an_empty_one():
    router = McpNotificationRouter()
    seen: list[dict[str, Any]] = []
    router.handle(METHOD, lambda server, params: seen.append(params))
    send, receive = _stream(
        _notification(METHOD), _notification(METHOD, {"a": 1}), _notification(METHOD, {})
    )
    send.close()

    with pytest.raises(anyio.EndOfStream):
        await router.tap("acme", receive).receive()
    assert seen == [{}, {"a": 1}, {}]


async def test_requests_responses_exceptions_and_unregistered_notifications_pass_through():
    router = McpNotificationRouter()
    router.handle(METHOD, lambda server, params: pytest.fail("not for this one"))
    request = SessionMessage(JSONRPCRequest(jsonrpc="2.0", id=1, method=METHOD))
    response = SessionMessage(JSONRPCResponse(jsonrpc="2.0", id=1, result={}))
    problem = RuntimeError("transport")
    cancelled = _notification("notifications/cancelled", {"requestId": 1})
    send, receive = _stream(request, response, problem, cancelled)
    send.close()  # so a message wrongly diverted ends the stream instead of hanging it
    tapped = router.tap("acme", receive)

    got = [await tapped.receive() for _ in range(4)]

    assert got[0] is request and got[1] is response and got[2] is problem and got[3] is cancelled


async def test_a_handler_that_raises_is_logged_and_the_stream_goes_on(caplog):
    router = McpNotificationRouter()

    def broken(server: str, params: dict[str, Any]) -> None:
        raise ValueError("bad handler")

    router.handle(METHOD, broken)
    after = _notification("notifications/progress", {})
    _, receive = _stream(_notification(METHOD, {}), after)

    assert await router.tap("acme", receive).receive() is after
    assert any(
        "handling notifications/igni/notify from acme failed" in r.getMessage()
        for r in caplog.records
    )


async def test_an_unregistered_method_delivers_to_nobody():
    router = McpNotificationRouter()
    assert router.handles(METHOD) is False
    router.deliver("acme", METHOD, {})  # no handler, nothing raised


async def test_registering_a_method_again_replaces_the_handler():
    router = McpNotificationRouter()
    calls: list[str] = []
    router.handle(METHOD, lambda server, params: calls.append("first"))
    router.handle(METHOD, lambda server, params: calls.append("second"))

    assert router.handles(METHOD) is True
    router.deliver("acme", METHOD, {})
    assert calls == ["second"]


async def test_a_closed_inner_stream_ends_the_tapped_one_and_closing_reaches_it():
    router = McpNotificationRouter()
    send, receive = _stream()
    tapped = router.tap("acme", receive)

    send.close()
    with pytest.raises(anyio.EndOfStream):
        await tapped.receive()

    await tapped.aclose()
    with pytest.raises(anyio.ClosedResourceError):
        receive.receive_nowait()
