"""The edges of landing an item: what is refused, what is skipped, what opens a chat.

The real lander runs over a real database (the rig) with a played chat
desk. The main paths are in ``test_a_service_reaches_an_agent_in_its_own_chats``.
"""

from __future__ import annotations

import pytest

from igni_core.core.db.urls import DatabaseUrl
from igni_core.extensions.service_attachments import AttachmentUnavailable
from igni_core.extensions.service_landing import Lander, Mentions, PostRefused
from tests.service_item_rig import Files, Inbox, item, message

CLAIM = "claim:CL-1"


def _action(event_id: str, **fields):
    return item(event_id, kind="action", type="action", payload={}, **fields)


def _lander(rig: Inbox, *, files=None) -> Lander:
    return Lander(desk=rig.desk, threads=rig.threads, held=rig.held, files=files)


async def test_an_event_never_lands_in_a_chat(database_url: DatabaseUrl):
    rig = Inbox(database_url)

    with pytest.raises(ValueError, match="its extension's"):
        await _lander(rig).land(item("e1"))
    assert rig.desk.posts == [] and list(rig.desk.titles) == ["open"]


async def test_a_post_the_chat_refuses_raises_so_the_item_comes_again(database_url: DatabaseUrl):
    rig = Inbox(database_url)
    rig.desk.refuse = True

    with pytest.raises(PostRefused, match="the session is gone"):
        await _lander(rig).land(message("m1", text="x", thread=CLAIM))


async def test_files_with_nobody_to_fetch_them_raise_unavailable(database_url: DatabaseUrl):
    rig = Inbox(database_url)
    attached = message("m1", text="x", attachments=[{"attachment_id": "a", "filename": "f"}])

    with pytest.raises(AttachmentUnavailable, match="cannot fetch an item's files"):
        await _lander(rig, files=None).land(attached)
    assert rig.desk.posts == []


async def test_a_message_with_files_and_no_text_is_just_the_references(database_url: DatabaseUrl):
    rig = Inbox(database_url, files=Files(a=b"x"))
    await rig.deliver(
        message("m1", text=None, attachments=[{"attachment_id": "a", "filename": "f.txt"}])
    )

    [post] = rig.desk.posts
    assert post.text == f"\n\n@/project/.igni/attachments/{post.session_id}/f.txt"


async def test_start_run_false_with_files_queues_them_named_and_does_not_hold(
    database_url: DatabaseUrl,
):
    rig = Inbox(database_url, files=Files(a=b"x"))
    await rig.deliver(message("m0", text="open", thread=CLAIM))
    chat = rig.desk.posts[0].session_id
    rig.desk.running.add(chat)

    await rig.deliver(
        message(
            "m1",
            text="later",
            thread=CLAIM,
            start_run=False,
            attachments=[{"attachment_id": "a", "filename": "f.txt"}],
        )
    )

    last = rig.desk.posts[-1]
    assert last.next_run and "@" in last.text and rig.held.waiting() == []
    assert rig.desk.files, "the file is kept all the same"


async def test_a_chat_item_without_text_posts_nothing_even_with_files(database_url: DatabaseUrl):
    rig = Inbox(database_url, files=Files(a=b"x"))
    await rig.deliver(
        item(
            "c1",
            kind="chat",
            type="chat",
            payload={},
            thread=CLAIM,
            attachments=[{"attachment_id": "a", "filename": "f"}],
        )
    )
    assert rig.desk.posts == [] and rig.desk.files == {} and rig.files.fetched == []


async def test_cancel_for_an_unknown_thread_or_an_idle_chat_does_nothing(
    database_url: DatabaseUrl,
):
    rig = Inbox(database_url)
    await rig.deliver(_action("a1", thread="never-seen", action={"op": "cancel"}))
    assert rig.desk.cancelled == [] and list(rig.desk.titles) == ["open"], "opens nothing either"

    await rig.deliver(message("m0", text="start", thread=CLAIM))
    receipt = await rig.deliver(_action("a2", thread=CLAIM, action={"op": "cancel"}))
    assert rig.desk.cancelled == [] and receipt.delivered == ["a2"]


async def test_rename_for_an_unknown_thread_opens_the_chat_with_that_title(
    database_url: DatabaseUrl,
):
    rig = Inbox(database_url)
    await rig.deliver(_action("a1", thread=CLAIM, action={"op": "rename", "title": "Paid"}))

    assert list(rig.desk.titles.values())[1:] == ["Paid"]
    assert rig.threads.find("acme.claims", CLAIM).title == "Paid"  # type: ignore[union-attr]


async def test_show_for_an_unknown_thread_opens_its_default_chat_then_shows(
    database_url: DatabaseUrl,
):
    rig = Inbox(database_url)
    await rig.deliver(_action("a1", thread=CLAIM, action={"op": "show"}))

    [(chat, view_id, view)] = rig.desk.views
    assert rig.desk.titles[chat] == "acme.claims: claim:CL-1"
    assert (view_id, view) == ("svc-a1", {}), "no view is an empty one"


async def test_an_action_item_without_an_action_is_ignored_and_counts_delivered(
    database_url: DatabaseUrl,
):
    rig = Inbox(database_url)
    receipt = await rig.deliver(_action("a1", thread=CLAIM))

    assert receipt.delivered == ["a1"]
    assert rig.desk.views == [] and list(rig.desk.titles) == ["open"]


async def test_a_thread_with_colons_and_unicode_is_titled_as_it_is_spelled(
    database_url: DatabaseUrl,
):
    rig = Inbox(database_url)
    await rig.deliver(message("m1", text="x", thread="клейм:№1::x"))
    assert list(rig.desk.titles.values())[1:] == ["acme.claims: клейм:№1::x"]


def test_files_are_named_after_the_text_by_a_blank_line_and_a_space():
    assert Mentions.append("see", ["a", "b"]) == "see\n\n@a @b"
    assert Mentions.append("see", []) == "see"
    assert Mentions.append("", ["a"]) == "\n\n@a"
