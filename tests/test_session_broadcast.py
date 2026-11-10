"""Unit tests for ``session/broadcast.py`` and the event base classes.

Broadcast machinery was extracted from Session in iter 141 and
turned into the :class:`BroadcastBus` class in the OOP pivot for
the ``session/`` package. Session's own tests cover the delegating
facade methods; these pins guard the class contract itself —
idempotent register, exception isolation between subscribers, and
the run_id stamping policy on drain.
"""

from typing import Literal
from unittest.mock import MagicMock

from igni_core.core.broadcast_schema import BroadcastEvent, RunScopedBroadcastEvent
from igni_core.core.session.broadcast import BroadcastBus
from igni_core.core.tools.plan import PlanSubmittedEvent


class _PingEvent(BroadcastEvent):
    channel: Literal["ping"] = "ping"
    k: int = 1


class _ReEvent(BroadcastEvent):
    channel: Literal["re"] = "re"


def _bus_with_callback(cb):
    """Fresh bus wired with a single subscriber — most tests want
    this shape and building it inline noise-up the reads."""
    bus = BroadcastBus()
    bus.register(cb)
    return bus


class TestRegister:
    def test_registers_new(self):
        bus = BroadcastBus()
        cb = MagicMock()
        bus.register(cb)
        assert bus.has_callbacks

    def test_idempotent(self):
        # Same subscriber registered twice should only appear once
        # so a reload doesn't fan out events N times.
        bus = BroadcastBus()
        cb = MagicMock()
        bus.register(cb)
        bus.register(cb)
        bus.emit(_PingEvent())
        assert cb.call_count == 1


class TestEmit:
    def test_fires_all_callbacks_with_the_event(self):
        a, b = MagicMock(), MagicMock()
        bus = BroadcastBus()
        bus.register(a)
        bus.register(b)
        event = _PingEvent()
        bus.emit(event)
        a.assert_called_once_with(event)
        b.assert_called_once_with(event)
        assert a.call_args.args[0] is b.call_args.args[0] is event

    def test_no_callbacks_is_noop(self):
        # Bare bus should not raise. Common during session
        # bootstrap before any transport has subscribed.
        BroadcastBus().emit(_PingEvent())

    def test_one_callback_exception_does_not_sink_others(self):
        # Headline safety property: a bad plugin can't stop
        # legitimate FE clients from receiving the same event.
        bad = MagicMock(side_effect=RuntimeError("boom"))
        good = MagicMock()
        bus = BroadcastBus()
        bus.register(bad)
        bus.register(good)
        bus.emit(_PingEvent())
        good.assert_called_once()


class TestQueuePostRun:
    def test_appends_to_queue(self):
        bus = BroadcastBus()
        bus.queue_post_run(PlanSubmittedEvent(plan="x"))
        assert bus.pending_count == 1

    def test_queue_holds_until_drain(self):
        cb = MagicMock()
        bus = _bus_with_callback(cb)
        event = PlanSubmittedEvent(plan="x")
        bus.queue_post_run(event)
        # Nothing fires until drain — the whole point.
        cb.assert_not_called()
        bus.drain_post_run()
        cb.assert_called_once_with(event)


class TestDrainPostRun:
    def test_flushes_all(self):
        cb = MagicMock()
        bus = _bus_with_callback(cb)
        bus.queue_post_run(_PingEvent())
        bus.queue_post_run(PlanSubmittedEvent(plan="x"))
        bus.drain_post_run()
        assert cb.call_count == 2
        assert bus.pending_count == 0

    def test_empty_queue_is_noop(self):
        cb = MagicMock()
        bus = _bus_with_callback(cb)
        bus.drain_post_run()
        cb.assert_not_called()

    def test_stamps_run_id_into_payload(self):
        # The plan tool can't see the run_id from inside its toolkit
        # context; drain injects it via ``with_run_id``.
        cb = MagicMock()
        bus = _bus_with_callback(cb)
        bus.queue_post_run(PlanSubmittedEvent(plan="x"))
        bus.drain_post_run(run_id="run-42")
        (event,) = cb.call_args.args
        assert event.channel == "plan_submitted"
        assert event.to_payload() == {"run_id": "run-42", "plan": "x", "tasks": []}

    def test_unstamped_payload_has_no_run_id_key(self):
        cb = MagicMock()
        bus = _bus_with_callback(cb)
        bus.queue_post_run(PlanSubmittedEvent(plan="x"))
        bus.drain_post_run(run_id=None)
        (event,) = cb.call_args.args
        assert event.to_payload() == {"plan": "x", "tasks": []}

    def test_existing_run_id_preserved(self):
        cb = MagicMock()
        bus = _bus_with_callback(cb)
        bus.queue_post_run(PlanSubmittedEvent(plan="x", run_id="original"))
        bus.drain_post_run(run_id="different")
        (event,) = cb.call_args.args
        assert event.to_payload()["run_id"] == "original"

    def test_events_that_are_not_run_scoped_are_not_stamped(self):
        cb = MagicMock()
        bus = _bus_with_callback(cb)
        event = _PingEvent()
        bus.queue_post_run(event)
        bus.drain_post_run(run_id="run-42")
        cb.assert_called_once_with(event)
        assert "run_id" not in event.to_payload()

    def test_snapshot_before_clear_prevents_reentry_loop(self):
        # If a subscriber re-queues during drain, that entry stays
        # in the queue for the NEXT drain — it doesn't loop here.
        bus = BroadcastBus()
        received: list[str] = []

        def _requeuer(event):
            received.append(event.channel)
            bus.queue_post_run(_ReEvent())

        bus.register(_requeuer)
        bus.queue_post_run(_PingEvent())
        bus.drain_post_run()
        # First drain: original entry fired, re-queued one waits.
        assert received == ["ping"]
        assert bus.pending_count == 1
        # Second drain: the re-queued entry now fires.
        bus.drain_post_run()
        assert received[-1] == "re"


class TestBroadcastEvent:
    def test_to_payload_omits_channel(self):
        assert _PingEvent(k=3).to_payload() == {"k": 3}

    def test_base_with_run_id_is_a_noop(self):
        event = _PingEvent()
        assert event.with_run_id("R-1") is event


class TestRunScopedBroadcastEvent:
    def test_run_id_absent_until_stamped(self):
        assert "run_id" not in PlanSubmittedEvent(plan="x").to_payload()

    def test_with_run_id_stamps_when_missing(self):
        event = PlanSubmittedEvent(plan="x")
        stamped = event.with_run_id("R-1")
        assert isinstance(stamped, RunScopedBroadcastEvent)
        assert stamped is not event
        assert stamped.to_payload()["run_id"] == "R-1"
        # Source event is not mutated.
        assert event.run_id is None

    def test_with_run_id_preserves_existing(self):
        event = PlanSubmittedEvent(plan="x", run_id="keep")
        assert event.with_run_id("override") is event

    def test_with_run_id_noop_on_empty(self):
        event = PlanSubmittedEvent(plan="x")
        assert event.with_run_id(None) is event
        assert event.with_run_id("") is event
