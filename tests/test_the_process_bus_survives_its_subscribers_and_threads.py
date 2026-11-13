"""The process bus under awkward use: bad payloads, self-unsubscribing
subscribers, and callers on several threads."""

from __future__ import annotations

import logging
import threading

import pytest

from igni_core.core.tools.process_bus import ProcessEventBus
from igni_core.core.tools.process_events import ProcessLineEvent, ProcessStartEvent


def test_a_payload_the_bus_does_not_route_is_warned_about_and_dropped(caplog):
    bus = ProcessEventBus()
    seen: list[object] = []
    for kind in ("start", "line", "exit"):
        bus.on(kind, seen.append)
    bogus = ProcessLineEvent.model_construct(type="bogus", pid=1, line="x")

    with caplog.at_level(logging.WARNING, logger="igni_core.core.tools.process_bus"):
        bus.emit(bogus)

    assert seen == []
    assert "unknown event type 'bogus'" in caplog.text


def test_a_raising_subscriber_is_logged_by_event_and_message(caplog):
    bus = ProcessEventBus()

    def bad(_p: ProcessLineEvent) -> None:
        raise RuntimeError("flaky push")

    bus.on("line", bad)

    with caplog.at_level(logging.WARNING, logger="igni_core.core.tools.process_bus"):
        bus.emit(ProcessLineEvent(pid=1, line="x"))

    assert "line subscriber raised: flaky push" in caplog.text


def test_a_subscriber_that_unsubscribes_itself_does_not_skip_the_next_one():
    bus = ProcessEventBus()
    order: list[str] = []

    def first(_p: ProcessLineEvent) -> None:
        order.append("first")
        bus.off("line", first)

    def second(_p: ProcessLineEvent) -> None:
        order.append("second")

    bus.on("line", first)
    bus.on("line", second)

    bus.emit(ProcessLineEvent(pid=1, line="a"))
    bus.emit(ProcessLineEvent(pid=1, line="b"))

    assert order == ["first", "second", "second"]
    assert bus.subscriber_count("line") == 1


def test_a_subscriber_may_register_another_without_deadlocking():
    bus = ProcessEventBus()
    late: list[ProcessLineEvent] = []

    def adds_one(_p: ProcessLineEvent) -> None:
        bus.on("line", late.append)

    bus.on("line", adds_one)
    bus.emit(ProcessLineEvent(pid=1, line="a"))

    assert late == []
    bus.emit(ProcessLineEvent(pid=1, line="b"))
    assert [e.line for e in late] == ["b"]


def test_unknown_names_are_refused_on_register_and_ignored_elsewhere():
    bus = ProcessEventBus()

    with pytest.raises(ValueError, match="unknown event type: 'nope'"):
        bus.on("nope", lambda _p: None)  # type: ignore[call-overload]
    bus.off("nope", lambda _p: None)  # type: ignore[call-overload]
    assert bus.subscriber_count("nope") == 0  # type: ignore[arg-type]


def test_threads_registering_unregistering_and_emitting_lose_nothing():
    bus = ProcessEventBus()
    lock = threading.Lock()
    stable_calls = 0

    def stable(_p: ProcessLineEvent) -> None:
        nonlocal stable_calls
        with lock:
            stable_calls += 1

    bus.on("line", stable)
    errors: list[BaseException] = []

    def churn(n: int) -> None:
        try:
            for _ in range(200):
                cb = lambda _p, n=n: None  # noqa: E731
                bus.on("line", cb)
                bus.off("line", cb)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def emit() -> None:
        try:
            for i in range(200):
                bus.emit(ProcessLineEvent(pid=1, line=str(i)))
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=churn, args=(i,)) for i in range(4)]
    threads += [threading.Thread(target=emit) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert stable_calls == 4 * 200
    assert bus.subscriber_count("line") == 1


def test_a_start_event_reaches_its_subscriber_as_the_typed_model():
    bus = ProcessEventBus()
    got: list[ProcessStartEvent] = []
    bus.on("start", got.append)

    event = ProcessStartEvent(pid=9, cmd="make", started_at=2.0)
    bus.emit(event)

    assert got == [event]
    assert isinstance(got[0], ProcessStartEvent)
