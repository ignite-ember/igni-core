"""Tests for the composite transport — the fan-out over several transports.

What the composite owes the rest of the backend: one message out goes
to every view, one message in comes from every view, a child that
misbehaves does not take the others down with it, and nothing is
swallowed in silence.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable

import pytest

from igni_core.protocol import messages as msg
from igni_core.transport.base import ListeningTransport
from igni_core.transport.composite import CompositeTransport
from igni_core.transport.in_process import InProcessTransport

_COMPOSITE_LOG = "igni_core.transport.composite"


class _Child(ListeningTransport):
    """A child transport that records what the composite asked of it."""

    def __init__(
        self,
        *,
        name: str,
        client_count: int = 0,
        owned: str | None = None,
        fail_send: bool = False,
        fail_close: bool = False,
        ever_connects: bool = True,
    ) -> None:
        self.name = name
        self.sent: list[msg.Message] = []
        self.closed = False
        self.started = False
        self.receive_calls = 0
        self.watchers: list[Callable[[str, str], None]] = []
        self._client_count = client_count
        self._owned = owned
        self._fail_send = fail_send
        self._fail_close = fail_close
        self._ever_connects = ever_connects
        self._inbox: asyncio.Queue[msg.Message | None] = asyncio.Queue()

    async def send(self, message: msg.Message) -> None:
        if self._fail_send:
            raise RuntimeError(f"{self.name} is wedged")
        self.sent.append(message)

    async def receive(self) -> AsyncIterator[msg.Message]:
        self.receive_calls += 1
        while True:
            item = await self._inbox.get()
            if item is None:
                break
            yield item

    async def close(self) -> None:
        if self._fail_close:
            raise RuntimeError(f"{self.name} refuses to close")
        self.closed = True
        await self._inbox.put(None)

    @property
    def is_closed(self) -> bool:
        return self.closed

    async def start(self) -> None:
        self.started = True

    async def wait_for_connection(self, timeout: float | None = 30.0) -> None:
        if not self._ever_connects:
            await asyncio.Event().wait()

    @property
    def client_count(self) -> int:
        return self._client_count

    def owns_client(self, client_id: str) -> bool:
        return client_id == self._owned

    def watch_connections(self, watcher: Callable[[str, str], None]) -> None:
        self.watchers.append(watcher)


async def test_a_child_that_fails_to_send_does_not_silence_the_others(caplog):
    """One wedged view is not the whole backend's problem — and it is not silent."""
    wedged = _Child(name="wedged", fail_send=True)
    healthy = _Child(name="healthy")
    composite = CompositeTransport([wedged, healthy])

    with caplog.at_level(logging.WARNING, logger=_COMPOSITE_LOG):
        await composite.send(msg.Info(text="for everyone"))

    assert [m.text for m in healthy.sent] == ["for everyone"]
    assert wedged.sent == []
    assert any("_Child failed to send" in r.getMessage() for r in caplog.records)


async def test_a_child_that_fails_to_close_does_not_stop_the_others(caplog):
    """The rest of the children still get closed, and the failure is logged."""
    wedged = _Child(name="wedged", fail_close=True)
    healthy = _Child(name="healthy")
    composite = CompositeTransport([wedged, healthy])

    with caplog.at_level(logging.WARNING, logger=_COMPOSITE_LOG):
        await composite.close()

    assert composite.is_closed
    assert healthy.closed
    assert any("_Child failed to close" in r.getMessage() for r in caplog.records)


async def test_the_composite_answers_only_for_the_children_that_know_clients():
    """A child that carries messages but no connections counts for nothing."""
    fe_side, plain = InProcessTransport.create_pair()
    ws_child = _Child(name="ws", client_count=2, owned="ws-1")
    composite = CompositeTransport([ws_child, plain])

    assert composite.client_count == 2
    assert composite.owns_client("ws-1")
    assert not composite.owns_client("ws-gone")

    composite.watch_connections(lambda event, client_id: None)
    assert len(ws_child.watchers) == 1

    await composite.close()
    await fe_side.close()


async def test_a_wait_that_times_out_starts_no_merge_pumps():
    """Nothing attached, so nothing is read — and the wait says so."""
    child = _Child(name="silent", ever_connects=False)
    composite = CompositeTransport([child])

    with pytest.raises(asyncio.TimeoutError):
        await composite.wait_for_connection(timeout=0.05)

    assert child.receive_calls == 0
    await composite.close()


async def test_a_wait_that_succeeds_starts_the_merge_pumps():
    """The moment a client attaches, the children's inbound streams are merged."""
    child = _Child(name="attaching")
    composite = CompositeTransport([child])

    await composite.wait_for_connection(timeout=1.0)
    await asyncio.sleep(0.05)

    assert child.receive_calls == 1
    await composite.close()
