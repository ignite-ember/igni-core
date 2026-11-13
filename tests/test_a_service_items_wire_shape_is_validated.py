"""What a service may send as an item, and what igni refuses or fills in.

``ServiceEvent`` and the models it carries are the wire shape of
``/events/pull``: a newer igni server may send fields this igni does not
know, and must not be able to send a kind, an op or a view this igni
cannot act on.
"""

from __future__ import annotations

from datetime import UTC

import pytest
from pydantic import ValidationError

from igni_core.extensions.service_events import ServiceEvent
from igni_core.extensions.service_items import ChatAction, ItemAttachment, ItemKind


def test_the_kinds_are_the_strings_on_the_wire():
    assert [kind.value for kind in ItemKind] == ["event", "message", "chat", "action"]
    assert ItemKind("chat") is ItemKind.CHAT


def test_an_attachment_needs_an_id_and_a_name_and_defaults_the_rest():
    attachment = ItemAttachment.model_validate({"attachment_id": "a1", "filename": "f.txt"})

    assert (attachment.content_type, attachment.size, attachment.sha256) == (
        "application/octet-stream",
        0,
        "",
    )
    for missing in ({"filename": "f.txt"}, {"attachment_id": "a1"}):
        with pytest.raises(ValidationError):
            ItemAttachment.model_validate(missing)


def test_a_field_a_newer_igni_server_adds_is_ignored():
    attachment = ItemAttachment.model_validate(
        {"attachment_id": "a1", "filename": "f.txt", "virus_scan": "clean"}
    )
    action = ChatAction.model_validate({"op": "cancel", "reason": "stale"})

    assert not hasattr(attachment, "virus_scan") and action.op == "cancel"


@pytest.mark.parametrize("op", ["show", "rename", "cancel"])
def test_an_action_may_show_rename_or_cancel(op: str):
    assert ChatAction.model_validate({"op": op}).op == op


@pytest.mark.parametrize("op", ["delete", "", "SHOW"])
def test_an_action_that_is_none_of_those_is_refused(op: str):
    with pytest.raises(ValidationError):
        ChatAction.model_validate({"op": op})


def test_an_action_view_is_an_object_of_any_json():
    assert ChatAction.model_validate({"op": "show", "view": {"a": [1, {"b": None}]}}).view == {
        "a": [1, {"b": None}]
    }
    with pytest.raises(ValidationError):
        ChatAction.model_validate({"op": "show", "view": ["not", "an", "object"]})


def test_an_item_is_an_event_for_its_extension_unless_it_says_otherwise():
    event = ServiceEvent.model_validate({"event_id": "e1", "extension_id": "acme.claims"})

    assert event.kind is ItemKind.EVENT and event.start_run is True
    assert event.payload == {} and event.attachments == []
    assert (event.thread, event.text, event.title, event.action) == (None, None, None, None)
    assert (event.event_key, event.created_at, event.expires_at) == (None, None, None)
    assert event.client_id == "" and event.type == ""


def test_an_item_of_an_unknown_kind_or_without_its_ids_is_refused():
    for bad in (
        {"event_id": "e1", "extension_id": "x", "kind": "broadcast"},
        {"extension_id": "x"},
        {"event_id": "e1"},
    ):
        with pytest.raises(ValidationError):
            ServiceEvent.model_validate(bad)


def test_an_items_times_keep_their_zone():
    event = ServiceEvent.model_validate(
        {
            "event_id": "e1",
            "extension_id": "x",
            "created_at": "2026-05-01T10:00:00Z",
            "expires_at": "2026-05-01T12:00:00+02:00",
        }
    )

    assert event.created_at is not None and event.created_at.tzinfo is not None
    assert event.created_at.utcoffset() == UTC.utcoffset(None)
    assert event.expires_at is not None and event.expires_at.utcoffset().total_seconds() == 7200  # type: ignore[union-attr]
