"""Messages igni mirrors to every client of a session: who typed, what was
accepted and from whom, and which approval was answered."""

from __future__ import annotations

from igni_core.protocol.schemas.envelope import Direction
from igni_core.protocol.schemas.mirroring import (
    RequirementDetails,
    RequirementResolved,
    Typing,
    UserMessageReceived,
    Welcome,
)


def test_each_message_has_its_type_word_and_direction():
    expected = {
        Welcome: ("welcome", Direction.TO_CLIENT),
        Typing: ("typing", Direction.BOTH),
        UserMessageReceived: ("user_message_received", Direction.TO_CLIENT),
        RequirementResolved: ("requirement_resolved", Direction.TO_CLIENT),
        RequirementDetails: ("requirement_details", Direction.TO_CLIENT),
    }

    for cls, (word, direction) in expected.items():
        assert cls().type == word
        assert cls.direction is direction


def test_a_welcome_carries_the_id_igni_gave_the_client():
    assert Welcome(client_id="c7").model_dump()["client_id"] == "c7"
    assert Welcome().client_id == ""


def test_an_empty_draft_clears_what_was_typed():
    assert Typing(text="", client_id="c1").text == ""


def test_a_draft_is_the_whole_text_with_its_client():
    typing = Typing.model_validate_json('{"type": "typing", "text": "h\\u00e9", "client_id": "c1"}')

    assert (typing.text, typing.client_id) == ("hé", "c1")


def test_a_received_message_defaults_to_the_persons_own_and_not_queued():
    received = UserMessageReceived(text="hi")

    assert received.client_id == ""
    assert received.queued is False
    assert received.extension_id == ""
    assert received.service_client_id == ""


def test_a_message_an_extension_posted_names_the_extension_and_its_service():
    received = UserMessageReceived(text="hi", extension_id="demo", service_client_id="svc-1")

    again = UserMessageReceived.model_validate_json(received.model_dump_json())

    assert (again.extension_id, again.service_client_id) == ("demo", "svc-1")
    assert again.queued is False


def test_a_message_sent_during_a_run_says_it_was_queued():
    assert UserMessageReceived(text="x", queued=True).model_dump()["queued"] is True


def test_a_resolved_requirement_names_the_approval_to_close():
    assert RequirementResolved(requirement_id="q1").model_dump()["requirement_id"] == "q1"


def test_requirement_details_carry_the_reason_for_the_approval():
    details = RequirementDetails(requirement_id="q1", details="deletes a file")

    assert details.model_dump()["details"] == "deletes a file"
    assert RequirementDetails().details == ""


def test_an_unknown_field_is_ignored():
    assert "extra" not in Typing.model_validate({"type": "typing", "extra": 1}).model_dump()
