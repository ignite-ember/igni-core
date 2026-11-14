"""The table an extension's notices live in: once per event, bounded, marked where they are kept.

What a store answers for its listing, its counts and each way of
marking read, down to the number of rows a mark changed.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from igni_core.notifications.local_store import LocalNotificationStore
from igni_core.notifications.schemas import (
    ExtensionNotice,
    NotificationCounts,
    NotificationOrigin,
    NotificationTarget,
)


def notice(event_key: str, title: str = "Runbook.pdf") -> ExtensionNotice:
    return ExtensionNotice(
        kind="export_ready",
        title=title,
        body="ready",
        target=NotificationTarget(page="acme.scan", params={"export": event_key}),
        event_key=event_key,
    )


@pytest.fixture
def store(tmp_path) -> LocalNotificationStore:
    return LocalNotificationStore(tmp_path / "notifications.db")


def test_a_kept_notice_is_unread_local_and_carries_a_local_id(store):
    kept = store.add("acme.scanner", notice("e1"))

    assert kept is not None
    assert kept.id.startswith("local-")
    assert kept.origin is NotificationOrigin.LOCAL
    assert kept.source == "acme.scanner"
    assert kept.read is False
    assert kept.created_at is not None
    assert store.listing() == [
        kept.model_copy(update={"created_at": store.listing()[0].created_at})
    ]


def test_the_same_event_is_kept_once_whether_read_or_not(store):
    assert store.add("acme.scanner", notice("e1")) is not None
    assert store.add("acme.scanner", notice("e1", title="again")) is None
    store.mark_read()
    assert store.add("acme.scanner", notice("e1")) is None

    [only] = store.listing()
    assert only.title == "Runbook.pdf"
    assert only.read is True


def test_two_threads_telling_the_same_event_keep_it_once(store):
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: store.add("acme.scanner", notice("race")), range(8)))

    assert sum(result is not None for result in results) == 1
    assert len(store.listing()) == 1


def test_only_the_newest_are_kept_per_extension_and_other_extensions_are_left_alone(
    store, monkeypatch
):
    monkeypatch.setattr(LocalNotificationStore, "KEEP", 3)
    store.add("acme.quiet", notice("q1"))
    for n in range(5):
        store.add("acme.loud", notice(f"l{n}"))

    kept = {n.target.params["export"] for n in store.listing() if n.source == "acme.loud"}  # type: ignore[union-attr]

    assert kept == {"l2", "l3", "l4"}
    assert [n.source for n in store.listing() if n.source == "acme.quiet"] == ["acme.quiet"]
    assert store.counts().by_source == {"acme.loud": 3, "acme.quiet": 1}


def test_the_listing_is_newest_first_and_cut_at_its_limit(store):
    for n in range(5):
        store.add("acme.scanner", notice(f"e{n}"))

    assert [n.target.params["export"] for n in store.listing()] == [  # type: ignore[union-attr]
        "e4",
        "e3",
        "e2",
        "e1",
        "e0",
    ]
    assert [n.target.params["export"] for n in store.listing(limit=2)] == ["e4", "e3"]  # type: ignore[union-attr]
    assert store.listing(limit=0) == []


def test_text_of_any_length_and_script_comes_back_as_it_was_told(store):
    title = "导出完成 " + "é" * 190
    body = "x" * 1000
    store.add(
        "acme.scanner",
        ExtensionNotice(
            kind="k", title=title, body=body, target=NotificationTarget(page="p"), event_key="u"
        ),
    )

    [back] = store.listing()

    assert (back.title, back.body) == (title, body)


def test_the_counts_are_unread_only_and_per_source(store):
    store.add("a", notice("1"))
    store.add("a", notice("2"))
    store.add("b", notice("1"))
    store.mark_read(source="b")

    assert store.counts() == NotificationCounts(unread=2, by_source={"a": 2})
    store.mark_read()
    assert store.counts() == NotificationCounts()


def test_an_id_is_local_exactly_when_it_starts_local_dash(store):
    kept = store.add("a", notice("1"))
    assert kept is not None

    assert store.owns(kept.id)
    assert store.owns("local-")
    assert not store.owns("n-123")
    assert not store.owns("a-local-1")
    assert not store.owns("")


def test_marking_ids_changes_only_those_and_says_how_many(store):
    first = store.add("a", notice("1"))
    second = store.add("a", notice("2"))
    assert first is not None and second is not None

    assert store.mark_read(ids=[first.id, "server-id", "local-nobody"]) == 1
    assert store.mark_read(ids=[first.id]) == 0, "already read: nothing changed"
    assert {n.id: n.read for n in store.listing()} == {first.id: True, second.id: False}


def test_marking_no_ids_or_only_a_servers_changes_nothing(store):
    store.add("a", notice("1"))

    assert store.mark_read(ids=[]) == 0
    assert store.mark_read(ids=["server-id"]) == 0
    assert store.counts().unread == 1


def test_ids_win_over_a_source_given_with_them(store):
    mine = store.add("a", notice("1"))
    store.add("a", notice("2"))
    store.add("b", notice("1"))
    assert mine is not None

    assert store.mark_read(ids=[mine.id], source="b") == 1
    assert store.counts().by_source == {"a": 1, "b": 1}


def test_marking_a_source_marks_its_unread_and_marking_nothing_marks_all(store):
    store.add("a", notice("1"))
    store.add("a", notice("2"))
    store.add("b", notice("1"))

    assert store.mark_read(source="a") == 2
    assert store.mark_read(source="nobody") == 0
    assert store.mark_read() == 1
    assert store.mark_read() == 0
