"""The reminder queue hands each reminder over once, in the order it arrived.

Background ``asyncRewake`` hooks queue a text; the next turn drains them.
A hook that exits with code 2 but wrote nothing must not inject a blank
reminder, two drains never see the same reminder, and replacing the buffer
(when the hook executor is rebuilt) takes a copy.
"""

from __future__ import annotations

from igni_core.core.session.reminders import PendingReminderQueue


def test_reminders_drain_in_the_order_they_were_queued_and_only_once() -> None:
    queue = PendingReminderQueue()
    queue.queue("first")
    queue.queue("second")
    queue.queue("first")

    assert queue.pending == ["first", "second", "first"]
    assert queue.drain() == ["first", "second", "first"]
    assert queue.drain() == []
    assert queue.pending == []


def test_an_empty_reminder_is_not_queued() -> None:
    queue = PendingReminderQueue()

    queue.queue("")
    queue.queue("   ")

    assert queue.drain() == ["   "], "only a text of nothing at all is dropped"


def test_a_drained_list_is_a_snapshot_not_the_live_buffer() -> None:
    queue = PendingReminderQueue()
    queue.queue("a")

    drained = queue.drain()
    queue.queue("b")

    assert drained == ["a"]
    assert queue.drain() == ["b"]


def test_replacing_the_buffer_takes_a_copy_and_an_empty_list_clears_it() -> None:
    queue = PendingReminderQueue()
    queue.queue("stale")
    mine = ["x", "y"]

    queue.replace(mine)
    mine.append("leaked")

    assert queue.pending == ["x", "y"]

    queue.replace([])
    assert queue.drain() == []
