"""The thread map and the held posts, over a real database, as the delivery uses them.

``ServiceThreads`` is the durable (extension, thread) to session map;
``HeldPosts`` keeps posts for a chat whose run is going, and does not ask
the database again once a look found none.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from igni_core.core.db.database import Database
from igni_core.core.db.urls import DatabaseUrl
from igni_core.extensions.service_threads import HeldPost, HeldPosts, ServiceThreads, ThreadChat
from tests.storage_backends import Scopes


def _chat(extension_id: str = "acme.claims", thread: str = "t1", session_id: str = "s1"):
    return ThreadChat(extension_id=extension_id, thread=thread, session_id=session_id, title="T")


def _post(event_id: str, text: str = "hello", session_id: str = "s1") -> HeldPost:
    return HeldPost(
        event_id=event_id,
        session_id=session_id,
        extension_id="acme.claims",
        client_id="svc",
        text=text,
    )


def test_an_unknown_thread_or_extension_has_no_chat(database_url: DatabaseUrl):
    threads = ServiceThreads(Database(database_url, scope=Scopes.fresh()))
    threads.bind(_chat())

    assert threads.find("acme.claims", "t2") is None
    assert threads.find("acme.other", "t1") is None


def test_one_thread_key_under_two_extensions_is_two_chats(database_url: DatabaseUrl):
    threads = ServiceThreads(Database(database_url, scope=Scopes.fresh()))
    threads.bind(_chat("acme.a", session_id="sa"))
    threads.bind(_chat("acme.b", session_id="sb"))

    assert threads.find("acme.a", "t1").session_id == "sa"  # type: ignore[union-attr]
    assert threads.find("acme.b", "t1").session_id == "sb"  # type: ignore[union-attr]


def test_the_extensions_own_chat_is_the_empty_thread(database_url: DatabaseUrl):
    threads = ServiceThreads(Database(database_url, scope=Scopes.fresh()))
    threads.bind(_chat(thread=ServiceThreads.OWN, session_id="own"))

    found = threads.find("acme.claims", "")
    assert found is not None and (found.session_id, found.title) == ("own", "T")


def test_a_thread_bound_for_one_producer_is_not_found_by_another(database_url: DatabaseUrl):
    first, second = Scopes.pair(database_url)
    ServiceThreads(first).bind(_chat())

    assert ServiceThreads(first).find("acme.claims", "t1") is not None
    assert ServiceThreads(second).find("acme.claims", "t1") is None


def test_binding_a_bound_thread_again_is_refused_not_overwritten(database_url: DatabaseUrl):
    threads = ServiceThreads(Database(database_url, scope=Scopes.fresh()))
    threads.bind(_chat(session_id="first"))

    with pytest.raises(IntegrityError):
        threads.bind(_chat(session_id="second"))
    assert threads.find("acme.claims", "t1").session_id == "first"  # type: ignore[union-attr]


def test_retitling_changes_the_title_and_only_that_mapping(database_url: DatabaseUrl):
    threads = ServiceThreads(Database(database_url, scope=Scopes.fresh()))
    threads.bind(_chat(thread="t1"))
    threads.bind(_chat(thread="t2", session_id="s2"))

    threads.retitle("acme.claims", "t1", "Renamed")

    assert threads.find("acme.claims", "t1").title == "Renamed"  # type: ignore[union-attr]
    assert threads.find("acme.claims", "t2").title == "T"  # type: ignore[union-attr]


def test_retitling_an_unknown_mapping_creates_nothing_and_raises_nothing(
    database_url: DatabaseUrl,
):
    threads = ServiceThreads(Database(database_url, scope=Scopes.fresh()))

    threads.retitle("acme.claims", "ghost", "x")

    assert threads.find("acme.claims", "ghost") is None


def test_held_posts_wait_oldest_first(database_url: DatabaseUrl, monkeypatch: Any):
    held = HeldPosts(Database(database_url, scope=Scopes.fresh()))
    clock = iter([1.0, 3.0, 2.0])
    monkeypatch.setattr(
        "igni_core.extensions.service_threads.time", SimpleNamespace(time=lambda: next(clock))
    )
    for event_id in ("z-oldest", "a-newest", "m-middle"):
        held.hold(_post(event_id))

    assert [p.event_id for p in held.waiting()] == ["z-oldest", "m-middle", "a-newest"]


def test_holding_one_event_twice_keeps_the_first(database_url: DatabaseUrl):
    held = HeldPosts(Database(database_url, scope=Scopes.fresh()))
    held.hold(_post("e1", text="one"))
    held.hold(_post("e1", text="two"))

    assert [(p.event_id, p.text) for p in held.waiting()] == [("e1", "one")]


def test_releasing_removes_that_post_only_and_an_unknown_id_is_harmless(
    database_url: DatabaseUrl,
):
    held = HeldPosts(Database(database_url, scope=Scopes.fresh()))
    held.hold(_post("e1"))
    held.hold(_post("e2"))

    held.release("ghost")
    assert len(held.waiting()) == 2
    held.release("e1")
    assert [p.event_id for p in held.waiting()] == ["e2"]


def test_held_posts_are_one_producers_own(database_url: DatabaseUrl):
    first, second = Scopes.pair(database_url)
    HeldPosts(first).hold(_post("e1"))

    assert HeldPosts(second).waiting() == []
    HeldPosts(second).release("e1")
    assert len(HeldPosts(first).waiting()) == 1


def test_a_post_survives_a_restart_whole(database_url: DatabaseUrl):
    db = Database(database_url, scope=Scopes.fresh())
    text = "long " * 5000 + "ünïcode ✓"
    HeldPosts(db).hold(_post("e1", text=text))

    [post] = HeldPosts(db).waiting()
    assert post == _post("e1", text=text)


def test_after_an_empty_look_the_database_is_not_asked_until_something_is_held(
    database_url: DatabaseUrl, monkeypatch: Any
):
    db = Database(database_url, scope=Scopes.fresh())
    held = HeldPosts(db)
    assert held.waiting() == []

    asked: list[int] = []
    real = db.sync_session
    monkeypatch.setattr(db, "sync_session", lambda: (asked.append(1), real())[1])
    assert held.waiting() == [] and asked == []

    held.hold(_post("e1"))
    asked.clear()
    assert [p.event_id for p in held.waiting()] == ["e1"] and asked == [1]
