"""The notification shapes: what they accept, what they drop, and what they tell developers.

The types outside developers read in the generated SDKs carry a
description on every field; the cause of a change is one of five words.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from igni_core.notifications.schemas import (
    ExtensionNotice,
    FloorNotification,
    MarkReadRequest,
    NotificationCounts,
    NotificationsChanged,
    NotificationsListResult,
    NotificationsReadResult,
    NotificationTarget,
    PortalMe,
    ServerNotification,
)


def notice(**changes: object) -> dict[str, object]:
    params: dict[str, object] = {
        "kind": "k",
        "title": "t",
        "target": {"page": "p"},
        "event_key": "e",
    }
    params.update(changes)
    return params


# ── An extension's notice ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("field", "limit"), [("kind", 64), ("title", 200), ("event_key", 200), ("body", 1000)]
)
def test_a_notice_field_holds_exactly_its_limit(field, limit):
    assert getattr(ExtensionNotice.model_validate(notice(**{field: "x" * limit})), field)
    with pytest.raises(ValidationError):
        ExtensionNotice.model_validate(notice(**{field: "x" * (limit + 1)}))


@pytest.mark.parametrize("field", ["kind", "title", "event_key"])
def test_a_notice_needs_its_kind_title_and_key(field):
    with pytest.raises(ValidationError):
        ExtensionNotice.model_validate(notice(**{field: ""}))
    without = notice()
    del without[field]
    with pytest.raises(ValidationError):
        ExtensionNotice.model_validate(without)


def test_a_notice_may_have_no_body_and_ignores_what_it_does_not_know():
    parsed = ExtensionNotice.model_validate(notice(recipients=["x"], actor="y"))

    assert parsed.body == ""
    assert ExtensionNotice.model_validate(notice(body="")).body == ""
    assert not hasattr(parsed, "actor")


# ── Where a notification opens ──────────────────────────────────────


def test_a_target_needs_a_page():
    with pytest.raises(ValidationError):
        NotificationTarget.model_validate({"page": ""})
    with pytest.raises(ValidationError):
        NotificationTarget.model_validate({})


def test_params_become_strings_and_the_empty_ones_are_dropped():
    target = NotificationTarget.model_validate(
        {
            "page": "p",
            "params": {"n": 3, "on": False, "none": None, "obj": {"a": [1]}, 7: "seven", "s": "x"},
        }
    )

    assert target.params == {"n": "3", "on": "false", "obj": '{"a": [1]}', "7": "seven", "s": "x"}


def test_params_that_are_not_a_mapping_are_refused():
    with pytest.raises(ValidationError):
        NotificationTarget.model_validate({"page": "p", "params": ["a"]})


# ── What igni server sends ──────────────────────────────────────────


def test_a_row_from_a_newer_server_keeps_what_it_knows():
    row = ServerNotification.model_validate({"id": "n", "source": "s", "brand_new": 1})

    assert (row.org_id, row.kind, row.title, row.body) == ("", "", "", "")
    assert row.created_at is None
    assert row.target is None


def test_organisations_are_named_or_fall_back_to_their_id():
    me = PortalMe.model_validate(
        {"organizations": [{"id": "o1", "display_name": "Acme"}, {"id": "o2"}], "extra": 1}
    )

    assert me.names() == {"o1": "Acme", "o2": "o2"}
    assert PortalMe.model_validate({}).names() == {}


def test_counts_that_add_up_to_nothing_leave_the_source_out():
    one = NotificationCounts(unread=1, by_source={"a": 1, "gone": 0})
    other = NotificationCounts(unread=0, by_source={"gone": 0, "neg": -2, "a": 2})

    total = one.plus(other)

    assert total == NotificationCounts(unread=1, by_source={"a": 3})
    assert NotificationCounts().plus(NotificationCounts()) == NotificationCounts()


def test_marking_sends_only_what_was_set():
    assert MarkReadRequest().body() == {}
    assert MarkReadRequest(ids=[]).body() == {"ids": []}
    assert MarkReadRequest(source="s").body() == {"source": "s"}


# ── What the client sees ────────────────────────────────────────────


def test_a_server_row_with_no_time_or_actor_is_still_a_notification():
    row = ServerNotification.model_validate({"id": "n", "source": "s", "org_id": "o1"})

    shown = FloorNotification.from_server(row)

    assert shown.origin == "server"
    assert shown.created_at is None
    assert shown.actor == ""
    assert shown.read is False
    assert shown.org_name == ""


def test_an_organisation_is_named_only_among_several():
    row = ServerNotification.model_validate({"id": "n", "source": "s", "org_id": "o1"})

    one = FloorNotification.from_server(row, {"o1": "Acme"})
    two = FloorNotification.from_server(row, {"o1": "Acme", "o2": "Beta"})
    unknown = FloorNotification.from_server(row, {"o2": "Beta", "o3": "Gamma"})

    assert (one.org_id, one.org_name) == ("o1", "")
    assert (two.org_id, two.org_name) == ("o1", "Acme")
    assert (unknown.org_id, unknown.org_name) == ("o1", "")


def test_a_row_read_on_the_server_is_read():
    row = ServerNotification.model_validate(
        {"id": "n", "source": "s", "read_at": "2026-01-01T00:00:00Z", "actor": "ada"}
    )

    shown = FloorNotification.from_server(row)

    assert shown.read is True
    assert shown.actor == "ada"


# ── What a change says ──────────────────────────────────────────────


@pytest.mark.parametrize("cause", ["server", "listed", "marked_read", "extension", "signed_out"])
def test_a_change_is_one_of_five_causes(cause):
    assert NotificationsChanged(cause=cause).cause == cause


def test_a_change_with_another_cause_is_refused_and_the_default_is_the_server():
    with pytest.raises(ValidationError):
        NotificationsChanged(cause="timer")  # type: ignore[arg-type]
    assert NotificationsChanged().cause == "server"
    assert NotificationsChanged().counts == NotificationCounts()


def test_the_published_cause_lists_the_five_words():
    schema = NotificationsChanged.model_json_schema()

    assert schema["properties"]["cause"]["enum"] == [
        "server",
        "listed",
        "marked_read",
        "extension",
        "signed_out",
    ]


# ── What developers read ────────────────────────────────────────────


@pytest.mark.parametrize(
    "model",
    [
        NotificationTarget,
        NotificationCounts,
        FloorNotification,
        NotificationsListResult,
        NotificationsReadResult,
        NotificationsChanged,
    ],
)
def test_every_published_field_says_what_it_is(model: type[BaseModel]):
    missing = [name for name, field in model.model_fields.items() if not field.description]

    assert missing == []


def test_the_change_names_each_cause_in_its_description():
    description = NotificationsChanged.model_fields["cause"].description or ""

    for cause in ("server", "listed", "marked_read", "extension", "signed_out"):
        assert f"`{cause}`" in description
