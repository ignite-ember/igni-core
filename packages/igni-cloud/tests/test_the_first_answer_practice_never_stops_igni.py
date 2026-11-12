"""``igni_cloud/guest/practice.py``: a practice that fails says so and clears up."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from igni_cloud.guest.practice import FirstAnswerPractice
from websockets.asyncio.server import ServerConnection, serve

Handler = Callable[[ServerConnection], Awaitable[None]]


class Listener:
    """A transport as ``FirstAnswerPractice`` sees one: a port, a receive, a forget."""

    def __init__(self, port: int, *, forgets: bool = True) -> None:
        self.port = port
        self.forgotten = 0
        self.receiving = False
        self.stopped_receiving = False
        if forgets:
            self.forget_past_connections = self._forget

    def _forget(self) -> None:
        self.forgotten += 1

    async def receive(self) -> AsyncIterator[Any]:
        self.receiving = True
        try:
            await asyncio.Event().wait()
            yield None
        finally:
            self.stopped_receiving = True

    async def send(self, _message: Any) -> None:
        return None


async def _serving(handler: Handler) -> Any:
    return await serve(handler, "127.0.0.1", 0, compression=None)


def _port(server: Any) -> int:
    return server.sockets[0].getsockname()[1]


async def test_events_before_the_answer_are_skipped_and_the_request_is_a_windows_first():
    seen: list[dict[str, Any]] = []

    async def handler(ws: ServerConnection) -> None:
        await ws.send(json.dumps({"type": "welcome"}))
        request = json.loads(await ws.recv())
        seen.append(request)
        await ws.send(json.dumps({"type": "event", "id": "something-else"}))
        await ws.send(json.dumps({"type": "event"}))
        await asyncio.sleep(0.15)
        await ws.send(json.dumps({"type": "rpc_response", "id": request["id"]}))

    server = await _serving(handler)
    try:
        transport = Listener(_port(server))
        result = await FirstAnswerPractice(transport).run()
    finally:
        server.close()
    assert result.ok, result.error
    assert result.answer_ms is not None and result.answer_ms >= 150, "waited for its own answer"
    assert seen[0]["method"] == "get_session_id"
    assert seen[0]["id"] == FirstAnswerPractice.REQUEST_ID


async def test_a_wrapped_transport_is_practised_on_the_port_of_the_one_inside():
    async def handler(ws: ServerConnection) -> None:
        await ws.send(json.dumps({"type": "welcome"}))
        request = json.loads(await ws.recv())
        await ws.send(json.dumps({"id": request["id"]}))

    server = await _serving(handler)
    try:
        inner = Listener(_port(server))
        result = await FirstAnswerPractice(
            SimpleNamespace(inner=inner, receive=inner.receive)
        ).run()
    finally:
        server.close()
    assert result.ok, result.error
    assert inner.forgotten == 1


async def test_an_answer_that_never_comes_ends_at_the_timeout_and_clears_up(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(FirstAnswerPractice, "TIMEOUT_SECONDS", 0.2)

    async def handler(ws: ServerConnection) -> None:
        await ws.send(json.dumps({"type": "welcome"}))
        await ws.recv()
        await asyncio.sleep(5)

    server = await _serving(handler)
    try:
        transport = Listener(_port(server))
        result = await FirstAnswerPractice(transport).run()
    finally:
        server.close()
    assert not result.ok
    assert result.error.startswith("TimeoutError")
    assert result.welcome_ms is None and result.answer_ms is None
    assert transport.receiving and transport.stopped_receiving, "the answering ended with it"
    assert transport.forgotten == 1


async def test_a_connection_closed_early_is_a_failed_practice_not_an_exception():
    async def handler(ws: ServerConnection) -> None:
        await ws.close()

    server = await _serving(handler)
    try:
        transport = Listener(_port(server))
        result = await FirstAnswerPractice(transport).run()
    finally:
        server.close()
    assert not result.ok
    assert result.error.startswith("ConnectionClosed")
    assert transport.stopped_receiving
    assert transport.forgotten == 1


async def test_a_refused_connection_names_its_error():
    server = await _serving(lambda ws: asyncio.sleep(0))
    port = _port(server)
    server.close()
    await server.wait_closed()
    result = await FirstAnswerPractice(Listener(port)).run()
    assert not result.ok
    assert ": " in result.error and result.error.split(":")[0].endswith("Error")


async def test_a_transport_that_cannot_forget_is_practised_all_the_same():
    async def handler(ws: ServerConnection) -> None:
        await ws.send(json.dumps({"type": "welcome"}))
        request = json.loads(await ws.recv())
        await ws.send(json.dumps({"id": request["id"]}))

    server = await _serving(handler)
    try:
        result = await FirstAnswerPractice(Listener(_port(server), forgets=False)).run()
    finally:
        server.close()
    assert result.ok, result.error


async def test_a_transport_on_port_zero_listens_nowhere():
    result = await FirstAnswerPractice(SimpleNamespace(port=0)).run()
    assert not result.ok
    assert result.error == "the transport listens on no port"
