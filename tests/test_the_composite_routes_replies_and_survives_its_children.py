"""The composite's routing and its survival of misbehaving children.

A reply addressed to one connection reaches only the child that owns it
(never the terminal's socket, never everyone); a child whose stream
breaks does not end the merged stream; a close that is cancelled still
closes every child; and a wait with nothing to wait for returns.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable

import pytest

from igni_core.protocol import messages as msg
from igni_core.transport.base import ClientAwareTransport, ListeningTransport, Transport
from igni_core.transport.composite import CompositeTransport

_LOG = "igni_core.transport.composite"


class _Plain(Transport):
    """A client-only child: it carries messages but knows no connections."""

    def __init__(self) -> None:
        self.sent: list[msg.Message] = []
        self.closed = False
        self.inbox: asyncio.Queue[msg.Message | None] = asyncio.Queue()
        self.break_receive = False

    async def send(self, message: msg.Message) -> None:
        self.sent.append(message)

    async def receive(self) -> AsyncIterator[msg.Message]:
        if self.break_receive:
            raise RuntimeError("the stream broke")
        while True:
            item = await self.inbox.get()
            if item is None:
                return
            yield item

    async def close(self) -> None:
        self.closed = True
        await self.inbox.put(None)

    @property
    def is_closed(self) -> bool:
        return self.closed


class _Aware(_Plain, ClientAwareTransport):
    def __init__(self, owned: str | None = None) -> None:
        super().__init__()
        self._owned = owned
        self.watchers: list[Callable[[str, str], None]] = []

    @property
    def client_count(self) -> int:
        return 1

    def owns_client(self, client_id: str) -> bool:
        return client_id == self._owned

    def watch_connections(self, watcher: Callable[[str, str], None]) -> None:
        self.watchers.append(watcher)


class _Listening(_Aware, ListeningTransport):
    def __init__(self, owned: str | None = None, *, wait_error: BaseException | None = None):
        super().__init__(owned)
        self.started = 0
        self._wait_error = wait_error
        self.attach = asyncio.Event()

    async def start(self) -> None:
        self.started += 1

    async def wait_for_connection(self, timeout: float | None = 30.0) -> None:
        if self._wait_error is not None:
            raise self._wait_error
        await self.attach.wait()


def _addressed(text: str, client_id: str) -> msg.Message:
    reply = msg.Info(text=text)
    reply._to_client = client_id
    return reply


async def test_an_addressed_reply_reaches_only_the_child_that_owns_the_connection():
    plain = _Plain()
    other = _Aware(owned="ws-other")
    owner = _Aware(owned="ws-mine")
    composite = CompositeTransport([plain, other, owner])

    await composite.send(_addressed("for ws-mine", "ws-mine"))

    assert [m.text for m in owner.sent] == ["for ws-mine"]
    assert other.sent == [] and plain.sent == []


async def test_an_addressed_reply_nobody_owns_is_dropped_not_broadcast(caplog):
    plain = _Plain()
    aware = _Aware(owned="ws-1")
    composite = CompositeTransport([plain, aware])

    with caplog.at_level(logging.DEBUG, logger=_LOG):
        await composite.send(_addressed("lost", "ws-gone"))

    assert plain.sent == [] and aware.sent == []
    assert any("ws-gone" in r.getMessage() and "dropped" in r.getMessage() for r in caplog.records)


async def test_a_message_with_no_address_goes_to_every_child():
    plain, aware = _Plain(), _Aware(owned="ws-1")
    composite = CompositeTransport([plain, aware])

    await composite.send(msg.Info(text="everyone"))

    assert [m.text for m in plain.sent] == ["everyone"]
    assert [m.text for m in aware.sent] == ["everyone"]


async def test_start_binds_only_the_children_that_listen():
    plain, aware, listening = _Plain(), _Aware(), _Listening()
    composite = CompositeTransport([plain, aware, listening])

    await composite.start()

    assert listening.started == 1


async def test_watching_reaches_every_child_that_knows_its_views_and_no_other():
    plain, aware, listening = _Plain(), _Aware(), _Listening()
    composite = CompositeTransport([plain, aware, listening])

    def watcher(event: str, client_id: str) -> None:
        pass

    composite.watch_connections(watcher)

    assert aware.watchers == [watcher] and listening.watchers == [watcher]


async def test_a_wait_with_no_listening_child_returns_at_once_and_starts_no_pumps():
    plain = _Plain()
    composite = CompositeTransport([plain])

    await asyncio.wait_for(composite.wait_for_connection(timeout=None), 1)
    await asyncio.sleep(0.02)

    assert composite._pumps == []
    await composite.close()


async def test_a_wait_resolves_with_the_first_child_to_get_a_client_and_cancels_the_rest():
    quiet, busy = _Listening(), _Listening()
    composite = CompositeTransport([quiet, busy])

    waiting = asyncio.ensure_future(composite.wait_for_connection(timeout=5))
    await asyncio.sleep(0.02)
    assert not waiting.done()
    busy.attach.set()
    await asyncio.wait_for(waiting, 5)

    assert len(composite._pumps) == 2
    await composite.close()


async def test_a_child_that_fails_while_waited_on_is_logged_and_the_wait_still_ends(caplog):
    broken = _Listening(wait_error=RuntimeError("cannot listen"))
    composite = CompositeTransport([broken])

    with caplog.at_level(logging.WARNING, logger=_LOG):
        await asyncio.wait_for(composite.wait_for_connection(timeout=5), 5)

    assert any("cannot listen" in r.getMessage() for r in caplog.records)
    await composite.close()


async def test_a_child_whose_stream_breaks_does_not_end_the_merged_stream(caplog):
    broken, healthy = _Plain(), _Plain()
    broken.break_receive = True
    composite = CompositeTransport([broken, healthy])
    await healthy.inbox.put(msg.Info(text="from the healthy one"))

    async def first() -> msg.Message:
        async for m in composite.receive():
            return m
        raise AssertionError("the merged stream ended")

    with caplog.at_level(logging.INFO, logger=_LOG):
        got = await asyncio.wait_for(first(), 5)

    assert got.text == "from the healthy one"
    assert any("the stream broke" in r.getMessage() for r in caplog.records)
    await composite.close()


async def test_a_child_that_ends_its_stream_does_not_end_the_merged_stream():
    ending, healthy = _Plain(), _Plain()
    await ending.inbox.put(None)
    composite = CompositeTransport([ending, healthy])
    received: list[str] = []

    async def read() -> None:
        async for m in composite.receive():
            received.append(m.text)

    reader = asyncio.ensure_future(read())
    await asyncio.sleep(0.05)
    await healthy.inbox.put(msg.Info(text="later"))
    await asyncio.sleep(0.05)
    assert received == ["later"] and not reader.done()

    await composite.close()
    await asyncio.wait_for(reader, 5)


async def test_closing_ends_the_merged_stream_and_is_idempotent():
    child = _Plain()
    composite = CompositeTransport([child])
    reader = asyncio.ensure_future(_drain(composite))
    await asyncio.sleep(0.02)

    await composite.close()
    await composite.close()

    assert await asyncio.wait_for(reader, 5) == []
    assert composite.is_closed and child.closed


async def _drain(composite: CompositeTransport) -> list[msg.Message]:
    return [m async for m in composite.receive()]


async def test_a_close_that_is_cancelled_still_closes_every_child():
    stuck = _Plain()
    healthy = _Plain()
    composite = CompositeTransport([stuck, healthy])

    # A pump that ignores cancellation for a moment keeps the gather waiting.
    gate = asyncio.Event()

    async def slow_pump() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await gate.wait()
            raise

    composite._pumps = [asyncio.ensure_future(slow_pump())]
    await asyncio.sleep(0)
    closing = asyncio.ensure_future(composite.close())
    await asyncio.sleep(0.02)
    closing.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert stuck.closed and healthy.closed
    assert composite.is_closed
