"""``BroadcastBus``: the parts of the delivery contract the first tests leave open.

``test_session_broadcast.py`` covers register, fan-out, isolation,
queue and drain. These pin ordering, a subscriber joining mid-emit, the
read-only accessors, the debug log of a failing subscriber and what
survives a drain.
"""

from __future__ import annotations

import logging
from typing import Literal

import pytest

from igni_core.core.broadcast_schema import BroadcastEvent
from igni_core.core.session.broadcast import BroadcastBus


class _Ping(BroadcastEvent):
    channel: Literal["ping"] = "ping"
    n: int = 0


def test_subscribers_receive_the_same_event_in_registration_order() -> None:
    bus = BroadcastBus()
    seen: list[tuple[str, BroadcastEvent]] = []
    for name in ("a", "b", "c"):
        bus.register(lambda event, name=name: seen.append((name, event)))
    event = _Ping()

    bus.emit(event)

    assert [name for name, _ in seen] == ["a", "b", "c"]
    assert all(received is event for _, received in seen)


def test_a_subscriber_that_registers_another_during_emit_gets_it_next_time_only() -> None:
    bus = BroadcastBus()
    late_seen: list[int] = []

    def late(event: BroadcastEvent) -> None:
        late_seen.append(1)

    def early(event: BroadcastEvent) -> None:
        bus.register(late)

    bus.register(early)

    bus.emit(_Ping())
    assert late_seen == []
    bus.emit(_Ping())
    assert late_seen == [1]


def test_a_failing_subscriber_is_logged_at_debug_with_its_channel_and_the_rest_still_hear(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = BroadcastBus()
    heard: list[BroadcastEvent] = []

    def broken(event: BroadcastEvent) -> None:
        raise RuntimeError("boom")

    bus.register(broken)
    bus.register(heard.append)

    with caplog.at_level(logging.DEBUG, logger="igni_core.core.session.broadcast"):
        bus.emit(_Ping())

    assert len(heard) == 1
    assert [r.getMessage() for r in caplog.records] == [
        "broadcast callback raised on channel ping: boom"
    ]
    assert caplog.records[0].levelno == logging.DEBUG


def test_a_subscriber_that_is_not_an_exception_is_not_swallowed() -> None:
    bus = BroadcastBus()

    def leave(event: BroadcastEvent) -> None:
        raise KeyboardInterrupt

    bus.register(leave)

    with pytest.raises(KeyboardInterrupt):
        bus.emit(_Ping())


def test_the_accessors_report_state() -> None:
    bus = BroadcastBus()
    assert bus.has_callbacks is False
    assert bus.pending_count == 0

    bus.register(lambda event: None)
    bus.queue_post_run(_Ping())
    bus.queue_post_run(_Ping())

    assert bus.has_callbacks is True
    assert bus.pending_count == 2

    bus.drain_post_run()
    assert bus.pending_count == 0


def test_the_snapshot_is_a_copy_the_bus_does_not_follow() -> None:
    bus = BroadcastBus()
    heard: list[BroadcastEvent] = []
    bus.register(heard.append)

    snapshot = bus.callbacks_snapshot()
    assert snapshot == [heard.append]
    snapshot.clear()
    snapshot.append(lambda event: None)

    assert bus.callbacks_snapshot() == [heard.append]
    bus.emit(_Ping())
    assert len(heard) == 1


def test_a_drain_delivers_in_queue_order_and_leaves_what_a_callback_queues_for_the_next() -> None:
    bus = BroadcastBus()
    order: list[int] = []

    def callback(event: BroadcastEvent) -> None:
        order.append(event.n)  # type: ignore[attr-defined]
        if event.n == 1:  # type: ignore[attr-defined]
            bus.queue_post_run(_Ping(n=99))

    bus.register(callback)
    bus.queue_post_run(_Ping(n=1))
    bus.queue_post_run(_Ping(n=2))

    bus.drain_post_run()

    assert order == [1, 2]
    assert bus.pending_count == 1
    bus.drain_post_run()
    assert order == [1, 2, 99]


def test_a_queued_event_is_not_delivered_until_the_drain() -> None:
    bus = BroadcastBus()
    heard: list[BroadcastEvent] = []
    bus.register(heard.append)

    bus.queue_post_run(_Ping())

    assert heard == []
    bus.drain_post_run(None)
    assert len(heard) == 1
