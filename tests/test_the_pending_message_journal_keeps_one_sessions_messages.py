"""``PendingMessageJournal`` is the run pipeline's view of the pending-message table.

A message is recorded before the run, then completed or interrupted; a retry
discards what is stale; the ids an interrupted run left are queued and discarded
once the resume is acknowledged. Real journal over a real store on SQLite, read
back through the store.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.backend.pending_message_journal import PendingMessageJournal
from igni_core.core.session.pending_messages import PendingMessageStore


@pytest.fixture
def store(tmp_path: Path) -> PendingMessageStore:
    return PendingMessageStore(tmp_path / "pending.db")


async def _texts(store: PendingMessageStore, session_id: str) -> list[str]:
    return [m.text for m in await store.alist_pending(session_id)]


async def test_a_recorded_message_is_pending_for_its_session_only(store):
    mine, other = PendingMessageJournal(store, "s1"), PendingMessageJournal(store, "s2")

    await mine.record("hello")
    await other.record("elsewhere")

    assert await _texts(store, "s1") == ["hello"]
    assert await _texts(store, "s2") == ["elsewhere"]


async def test_a_completed_message_is_no_longer_pending(store):
    journal = PendingMessageJournal(store, "s1")
    pending_id = await journal.record("hello")

    await journal.mark_completed(pending_id)

    assert await _texts(store, "s1") == []


async def test_an_interrupted_message_keeps_its_reason_and_error(store):
    journal = PendingMessageJournal(store, "s1")
    pending_id = await journal.record("hello")

    await journal.mark_interrupted(pending_id, "errored", "model unreachable")

    (row,) = await store.alist_interrupted("s1")
    assert (row.message_id, row.interrupted_reason, row.last_error) == (
        pending_id,
        "errored",
        "model unreachable",
    )


async def test_a_late_cancel_does_not_resurrect_a_completed_message(store):
    journal = PendingMessageJournal(store, "s1")
    pending_id = await journal.record("hello")
    await journal.mark_completed(pending_id)

    await journal.mark_interrupted(pending_id, "cancelled")

    assert await store.alist_interrupted("s1") == []
    assert await _texts(store, "s1") == []


async def test_discarding_all_removes_this_sessions_pending_and_interrupted_rows_only(store):
    journal = PendingMessageJournal(store, "s1")
    other = PendingMessageJournal(store, "s2")
    await journal.record("waiting")
    interrupted = await journal.record("cut short")
    await journal.mark_interrupted(interrupted, "cancelled")
    done = await journal.record("finished")
    await journal.mark_completed(done)
    await other.record("not mine")

    assert await journal.discard_all_for_session() == 2

    assert await _texts(store, "s1") == []
    assert await store.alist_interrupted("s1") == []
    assert await _texts(store, "s2") == ["not mine"]
    assert await other.discard_all_for_session() == 1


async def test_discarding_all_of_a_session_with_no_rows_is_zero(store):
    assert await PendingMessageJournal(store, "s1").discard_all_for_session() == 0


async def test_queued_drops_are_discarded_once_and_the_queue_empties(store):
    journal = PendingMessageJournal(store, "s1")
    first, second = await journal.record("a"), await journal.record("b")
    keep = await journal.record("c")

    journal.queue_drops([first, second])
    assert journal.queued_drop_ids == [first, second]
    await journal.drain_queued_drops()

    assert await _texts(store, "s1") == ["c"]
    assert journal.queued_drop_ids == []
    # A second drain has nothing to do and leaves the rest alone.
    await journal.drain_queued_drops()
    assert [m.message_id for m in await store.alist_pending("s1")] == [keep]


async def test_queueing_again_replaces_the_queue_instead_of_adding_to_it(store):
    journal = PendingMessageJournal(store, "s1")
    first, second = await journal.record("a"), await journal.record("b")

    journal.queue_drops([first])
    journal.queue_drops([second])
    await journal.drain_queued_drops()

    assert [m.message_id for m in await store.alist_pending("s1")] == [first]


def test_the_queue_is_copied_in_and_copied_out(store):
    journal = PendingMessageJournal(store, "s1")
    ids = ["x", "y"]

    journal.queue_drops(ids)
    ids.append("z")
    journal.queued_drop_ids.append("w")

    assert journal.queued_drop_ids == ["x", "y"]


async def test_draining_an_empty_queue_is_a_no_op(store):
    journal = PendingMessageJournal(store, "s1")
    await journal.record("a")

    await journal.drain_queued_drops()

    assert await _texts(store, "s1") == ["a"]
