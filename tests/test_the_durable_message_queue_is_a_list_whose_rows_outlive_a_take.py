"""The durable message queue behaves as the list its users treat it as, with a row behind each message.

``DurableMessageQueue`` is a ``MutableSequence`` of waiting texts. What it
adds to a list, pinned here on a real database:

* ``pop(0)`` (and the index that means the first) takes a message: the row
  stays, marked taken; any other index, ``del`` and ``clear`` drop rows
  without delivering;
* ``settle`` deletes the taken rows the conversation holds and never puts
  one back; ``recover`` also puts the others back at the front;
* a message put in at the front is first in memory and last in the database;
* a message that could not be written still waits, in memory, and is never
  marked taken; a queue that could not be read starts empty;
* storage failures are swallowed, rewriting an entry is refused.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from igni_core.core.session.queued_messages import (
    DurableMessageQueue,
    QueuedMessageStore,
    QueuedStatus,
    RecordedMessages,
)

SESSION = "s1"


@pytest.fixture
def store(tmp_path: Path) -> QueuedMessageStore:
    return QueuedMessageStore(tmp_path / "state.db")


def _queue(store: QueuedMessageStore, texts=(), session=SESSION) -> DurableMessageQueue:
    queue = DurableMessageQueue(store, session_id=lambda: session)
    for text in texts:
        queue.append(text)
    return queue


def _texts(store: QueuedMessageStore, status: QueuedStatus, session=SESSION) -> list[str]:
    return [text for _, text in store.rows(session, status)]


class Conversation:
    """Agno's copy of a session: it holds the messages with these ids."""

    def __init__(self, *ids: str, awaitable: bool = False) -> None:
        self.ids = ids
        self.awaitable = awaitable

    def get_session(self, **_):
        found = SimpleNamespace(
            runs=[SimpleNamespace(messages=[SimpleNamespace(id=i) for i in self.ids])]
        )
        if not self.awaitable:
            return found

        async def later():
            return found

        return later()


def test_the_queue_reads_as_a_list_in_the_order_it_was_written(store) -> None:
    queue = _queue(store, ["a", "b", "c"])

    assert len(queue) == 3
    assert (queue[0], queue[-1]) == ("a", "c")
    assert queue[1:] == ["b", "c"]
    assert list(queue) == ["a", "b", "c"]
    assert repr(queue) == "DurableMessageQueue(['a', 'b', 'c'])"
    assert _texts(store, QueuedStatus.WAITING) == ["a", "b", "c"]


def test_a_queue_loaded_later_holds_the_waiting_messages_in_order(store) -> None:
    _queue(store, ["a", "b"])
    _queue(store, ["other"], session="s2")

    assert list(_queue(store)) == ["a", "b"]


def test_pop_of_the_first_takes_it_and_keeps_its_row(store) -> None:
    queue = _queue(store, ["a", "b"])

    assert queue.pop(0) == "a"

    assert list(queue) == ["b"]
    assert _texts(store, QueuedStatus.TAKEN) == ["a"]
    assert _texts(store, QueuedStatus.WAITING) == ["b"]


def test_the_negative_index_of_the_first_is_also_a_take(store) -> None:
    queue = _queue(store, ["a", "b"])

    assert queue.pop(-2) == "a"

    assert _texts(store, QueuedStatus.TAKEN) == ["a"]


def test_pop_of_any_other_index_removes_without_taking(store) -> None:
    queue = _queue(store, ["a", "b", "c"])

    assert queue.pop() == "c"
    assert queue.pop(1) == "b"

    assert list(queue) == ["a"]
    assert _texts(store, QueuedStatus.TAKEN) == []
    assert _texts(store, QueuedStatus.WAITING) == ["a"]


def test_take_hands_over_the_row_id_the_conversation_will_hold(store) -> None:
    queue = _queue(store, ["a"])
    [(row_id, _)] = store.rows(SESSION, QueuedStatus.WAITING)

    taken = queue.take()

    assert (taken.id, taken.text) == (row_id, "a")


def test_taking_from_an_empty_queue_is_an_index_error(store) -> None:
    queue = _queue(store)

    with pytest.raises(IndexError):
        queue.take()
    with pytest.raises(IndexError):
        queue.pop(0)


def test_del_and_clear_drop_the_rows_without_delivering(store) -> None:
    queue = _queue(store, ["a", "b", "c", "d", "e"])

    del queue[0]
    del queue[1:3]
    assert list(queue) == ["b", "e"]
    assert _texts(store, QueuedStatus.WAITING) == ["b", "e"]

    queue.clear()
    assert len(queue) == 0
    assert _texts(store, QueuedStatus.WAITING) == []
    assert _texts(store, QueuedStatus.TAKEN) == []


def test_an_entry_cannot_be_rewritten(store) -> None:
    queue = _queue(store, ["a"])

    with pytest.raises(TypeError):
        queue[0] = "b"
    assert list(queue) == ["a"]


def test_a_message_put_at_the_front_waits_first_here_and_last_in_the_database(store) -> None:
    queue = _queue(store, ["a", "b"])

    queue.insert(0, "front")

    assert list(queue) == ["front", "a", "b"]
    assert list(_queue(store)) == ["a", "b", "front"]


def test_an_explicit_seq_orders_the_rows_and_a_tie_is_broken_by_id(store) -> None:
    late = store.add(SESSION, "late", seq=20)
    early = store.add(SESSION, "early", seq=10)
    tied = [store.add(SESSION, f"tie-{i}", seq=30) for i in range(3)]

    rows = store.rows(SESSION, QueuedStatus.WAITING)

    assert [i for i, _ in rows] == [early, late, *sorted(tied)]


def test_marking_nothing_changes_nothing(store) -> None:
    kept = store.add(SESSION, "a")

    store.mark_taken("nobody")
    store.mark_waiting([])
    store.remove([])
    store.remove(["nobody"])

    assert [i for i, _ in store.rows(SESSION, QueuedStatus.WAITING)] == [kept]


def test_a_message_that_could_not_be_written_still_waits_and_is_never_marked_taken(store) -> None:
    queue = _queue(store)
    with patch.object(store, "add", side_effect=RuntimeError("disk gone")):
        queue.append("unwritten")
    queue.append("written")

    assert list(queue) == ["unwritten", "written"]
    assert _texts(store, QueuedStatus.WAITING) == ["written"]

    with patch.object(store, "mark_taken") as marked:
        taken = queue.take()
    assert (taken.id, taken.text) == ("", "unwritten")
    marked.assert_not_called()

    [(written_id, _)] = store.rows(SESSION, QueuedStatus.WAITING)
    with patch.object(store, "remove") as removed:
        del queue[:]
    removed.assert_called_once_with([written_id])


def test_a_failed_take_write_still_delivers_the_message(store) -> None:
    queue = _queue(store, ["a"])

    with patch.object(store, "mark_taken", side_effect=RuntimeError("disk gone")):
        assert queue.pop(0) == "a"

    assert list(queue) == []


def test_a_queue_that_could_not_be_read_starts_empty(store) -> None:
    _queue(store, ["a"])

    with patch.object(store, "rows", side_effect=RuntimeError("disk gone")):
        assert list(DurableMessageQueue(store, session_id=lambda: SESSION)) == []


def test_a_drop_that_fails_is_swallowed(store) -> None:
    queue = _queue(store, ["a", "b"])

    with patch.object(store, "remove", side_effect=RuntimeError("disk gone")):
        assert queue.pop(1) == "b"
        queue.clear()

    assert len(queue) == 0


def test_the_session_is_asked_on_every_write(store) -> None:
    current = ["s1"]
    queue = DurableMessageQueue(store, session_id=lambda: current[0])

    queue.append("one")
    current[0] = "s2"
    queue.append("two")

    assert _texts(store, QueuedStatus.WAITING, "s1") == ["one"]
    assert _texts(store, QueuedStatus.WAITING, "s2") == ["two"]


async def test_settle_deletes_what_the_conversation_holds_and_leaves_the_rest_taken(store) -> None:
    queue = _queue(store, ["held", "not held yet", "waiting"])
    held = queue.take()
    queue.take()
    conversation = Conversation(held.id)

    await queue.settle(conversation)

    assert _texts(store, QueuedStatus.TAKEN) == ["not held yet"], "a paused run may still record it"
    assert _texts(store, QueuedStatus.WAITING) == ["waiting"]
    assert list(queue) == ["waiting"]


async def test_settle_with_nothing_taken_does_not_ask_the_conversation(store) -> None:
    queue = _queue(store, ["waiting"])

    asked: list[str] = []

    class Spy:
        def get_session(self, **_):
            asked.append("asked")

    await queue.settle(Spy())

    assert asked == []
    assert list(queue) == ["waiting"]


async def test_settle_and_recover_never_raise(store) -> None:
    queue = _queue(store, ["a"])
    queue.take()

    class Broken:
        def get_session(self, **_):
            raise RuntimeError("agno down")

    await queue.settle(Broken())
    assert await queue.recover(Broken()) == 0
    assert _texts(store, QueuedStatus.TAKEN) == ["a"]


async def test_recover_puts_the_unrecorded_back_at_the_front_in_their_order(store) -> None:
    queue = _queue(store, ["one", "two", "three", "four"])
    first = queue.take()
    queue.take()
    queue.take()
    # "four" still waits; "one" reached the conversation.
    after = DurableMessageQueue(store, session_id=lambda: SESSION)

    assert await after.recover(Conversation(first.id, awaitable=True)) == 2

    assert list(after) == ["two", "three", "four"]
    assert _texts(store, QueuedStatus.TAKEN) == []


async def test_recover_with_nothing_taken_is_zero(store) -> None:
    assert await _queue(store, ["a"]).recover(Conversation()) == 0


async def test_the_ids_a_conversation_holds_come_from_every_run(store) -> None:
    two_runs = SimpleNamespace(
        runs=[
            SimpleNamespace(messages=[SimpleNamespace(id="a"), SimpleNamespace(id=None)]),
            SimpleNamespace(messages=None),
            SimpleNamespace(messages=[SimpleNamespace(), SimpleNamespace(id=7)]),
        ]
    )

    class Sync:
        def get_session(self, **_):
            return two_runs

    assert await RecordedMessages.ids(Sync(), SESSION) == {"a", "7"}
    assert await RecordedMessages.ids(Conversation("x", "y", awaitable=True), SESSION) == {"x", "y"}

    class Missing:
        def get_session(self, **_):
            return None

    assert await RecordedMessages.ids(Missing(), SESSION) == set()
    assert await RecordedMessages.ids(Conversation(), SESSION) == set()
    runless = SimpleNamespace(runs=None)
    assert (
        await RecordedMessages.ids(SimpleNamespace(get_session=lambda **_: runless), SESSION)
        == set()
    )
