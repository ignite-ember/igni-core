"""The pending-message store's limits, its scope, and what it does when storage fails.

``PendingMessageStore`` is the durable record of a user's prompt from the
moment it arrives until its run completes. What the journal tests do not
reach:

* a list is capped at five rows, oldest first, and never shows a row of
  another org, producer or project, even for the same session id;
* a failure of the storage is swallowed where the caller is mid-shutdown
  (completing, discarding, interrupting, discarding all) and raised where
  the caller must tell someone (recording, listing);
* an unknown interrupted reason is refused before storage is touched;
* only an interrupted, still-pending row is discarded by id and session.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select

from igni_core.core.db.database import Database
from igni_core.core.db.scope import StorageScope, Tenant
from igni_core.core.session.pending_messages import PendingMessageStore, ReceivedMessageModel


@pytest.fixture
def store(tmp_path: Path) -> PendingMessageStore:
    return PendingMessageStore(tmp_path / "state.db")


def _clock(store: PendingMessageStore) -> None:
    """Every ``_now`` is one second after the last, so order is decided."""
    ticks = itertools.count(1000)
    store._now = lambda: next(ticks)  # type: ignore[method-assign]


def _broken(store: PendingMessageStore):
    return patch.object(store._db, "sync_session", side_effect=RuntimeError("disk gone"))


def test_a_list_of_pending_messages_is_the_oldest_five(store) -> None:
    _clock(store)
    ids = [store.record_received("s", f"m{i}") for i in range(7)]

    assert [m.message_id for m in store.list_pending("s")] == ids[:5]


def test_a_list_of_interrupted_messages_is_the_five_interrupted_first(store) -> None:
    _clock(store)
    ids = [store.record_received("s", f"m{i}") for i in range(7)]
    for message_id in reversed(ids):
        store.mark_interrupted(message_id, "cancelled")

    # Ordered by when they were interrupted, not when they arrived.
    assert [m.message_id for m in store.list_interrupted("s")] == list(reversed(ids))[:5]


def test_an_interrupted_message_is_still_a_pending_one(store) -> None:
    message_id = store.record_received("s", "text")
    store.mark_interrupted(message_id, "abandoned")

    assert [m.message_id for m in store.list_pending("s")] == [message_id]
    assert [m.interrupted_reason for m in store.list_interrupted("s")] == ["abandoned"]


def test_an_unknown_interrupted_reason_is_refused_before_storage_is_touched(store) -> None:
    with _broken(store) as touched, pytest.raises(ValueError, match="unknown interrupted_reason"):
        store.mark_interrupted("any", "vanished")  # type: ignore[arg-type]
    touched.assert_not_called()


def test_a_message_id_nobody_holds_changes_nothing(store) -> None:
    kept = store.record_received("s", "kept")

    store.mark_completed("nobody")
    store.mark_interrupted("nobody", "cancelled")
    store.discard("nobody")

    assert [m.message_id for m in store.list_pending("s")] == [kept]
    assert store.list_interrupted("s") == []


def test_a_message_text_comes_back_as_it_was_written(store) -> None:
    text = "héllo 世界 🙂 " + "x" * 50_000
    store.record_received("s", text)

    assert [m.text for m in store.list_pending("s")] == [text]


def test_ids_are_unique(store) -> None:
    assert len({store.record_received("s", "same") for _ in range(20)}) == 20


def test_another_org_producer_or_project_never_sees_or_touches_a_row(tmp_path: Path) -> None:
    url = tmp_path / "state.db"
    tenant = Tenant(org="org-a", producer="person:a")
    mine = PendingMessageStore(Database(url, scope=StorageScope.for_tenant(tenant, project="p")))
    others = [
        StorageScope.for_tenant(Tenant(org="org-b", producer="person:a"), project="p"),
        StorageScope.for_tenant(Tenant(org="org-a", producer="person:b"), project="p"),
        StorageScope.for_tenant(tenant, project="other-project"),
    ]
    message_id = mine.record_received("s", "mine")
    mine.mark_interrupted(message_id, "cancelled")

    for scope in others:
        theirs = PendingMessageStore(Database(url, scope=scope))
        assert theirs.list_pending("s") == []
        assert theirs.list_interrupted("s") == []
        theirs.mark_completed(message_id)
        theirs.discard(message_id)
        assert theirs.discard_interrupted("s", message_id) == 0
        assert theirs.discard_all_for_session("s") == 0
        theirs.record_received("s", "theirs")

    assert [m.text for m in mine.list_pending("s")] == ["mine"]
    assert [m.message_id for m in mine.list_interrupted("s")] == [message_id]


def test_discarding_an_interrupted_run_leaves_a_completed_one(store) -> None:
    done = store.record_received("s", "done")
    store.mark_completed(done)

    assert store.discard_interrupted("s", done) == 0
    assert store.discard_interrupted("s", "nobody") == 0
    # Still there: discarding all of the session does not take it either.
    assert store.discard_all_for_session("s") == 0
    with store._db.sync_session() as session:
        kept = session.scalars(select(ReceivedMessageModel.message_id)).all()
    assert kept == [done]


def test_a_discarded_message_is_gone(store) -> None:
    message_id = store.record_received("s", "text")

    store.discard(message_id)

    assert store.list_pending("s") == []


def test_a_storage_failure_is_swallowed_where_the_caller_is_mid_shutdown(store) -> None:
    message_id = store.record_received("s", "text")

    with _broken(store):
        assert store.mark_completed(message_id) is None
        assert store.discard(message_id) is None
        assert store.mark_interrupted(message_id, "errored", "boom") is None
        assert store.discard_all_for_session("s") == 0

    assert [m.message_id for m in store.list_pending("s")] == [message_id]
    assert store.list_interrupted("s") == []


def test_a_storage_failure_is_raised_where_the_caller_must_tell_someone(store) -> None:
    with _broken(store):
        with pytest.raises(RuntimeError, match="disk gone"):
            store.record_received("s", "text")
        with pytest.raises(RuntimeError, match="disk gone"):
            store.list_pending("s")
        with pytest.raises(RuntimeError, match="disk gone"):
            store.list_interrupted("s")
        with pytest.raises(RuntimeError, match="disk gone"):
            store.discard_interrupted("s", "m")


async def test_the_async_calls_do_what_the_sync_ones_do(store) -> None:
    first = await store.arecord_received("s", "one")
    second = await store.arecord_received("s", "two")
    third = await store.arecord_received("s", "three")

    await store.amark_completed(first)
    await store.amark_interrupted(second, "errored", "boom")
    await store.adiscard(third)

    assert await store.alist_pending("s") == store.list_pending("s")
    [interrupted] = await store.alist_interrupted("s")
    assert (interrupted.message_id, interrupted.interrupted_reason, interrupted.last_error) == (
        second,
        "errored",
        "boom",
    )
    assert await store.adiscard_interrupted("s", second) == 1
    other = await store.arecord_received("s", "four")
    assert await store.adiscard_all_for_session("s") == 1
    assert other not in [m.message_id for m in await store.alist_pending("s")]
