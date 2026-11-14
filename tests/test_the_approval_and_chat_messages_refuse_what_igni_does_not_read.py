"""The messages a client sends igni for the person, and what each one refuses.

An approval's ``action``, ``choice`` and ``set_permission_mode`` are closed
sets: an unlisted word is refused at the schema rather than read as a
confirmation, and an omitted action is a rejection.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.protocol.schemas.envelope import Direction
from igni_core.protocol.schemas.fe_actions import (
    Cancel,
    CancelLogin,
    Command,
    HITLDecision,
    HITLResponse,
    HITLResponseBatch,
    MCPToggle,
    ModelSwitch,
    QueueMessage,
    SessionList,
    SessionSwitch,
    Shutdown,
    StreamEnd,
    UserMessage,
)


@pytest.mark.parametrize("cls", [HITLResponse, HITLDecision])
class TestAnApprovalIsOneOfTheWordsIgniReads:
    def test_an_omitted_action_is_a_rejection(self, cls):
        assert cls().action == "reject"

    @pytest.mark.parametrize("action", ["approve", "yes", "CONFIRM", "", "allow"])
    def test_an_action_that_is_not_confirm_or_reject_is_refused(self, cls, action):
        with pytest.raises(ValidationError):
            cls(action=action)

    @pytest.mark.parametrize("action", ["confirm", "reject"])
    def test_confirm_and_reject_are_accepted(self, cls, action):
        assert cls.model_validate({"action": action}).action == action

    @pytest.mark.parametrize("choice", ["bogus", "ONCE", "forever", "allow"])
    def test_a_choice_outside_the_set_is_refused(self, cls, choice):
        with pytest.raises(ValidationError):
            cls(choice=choice)

    @pytest.mark.parametrize("choice", ["", "once", "always", "similar", "deny"])
    def test_each_listed_choice_is_accepted(self, cls, choice):
        assert cls.model_validate({"choice": choice}).choice == choice

    def test_an_omitted_choice_is_this_call_only(self, cls):
        assert cls().choice == ""


class TestAPermissionModeSwitchesOnlyToAKnownMode:
    @pytest.mark.parametrize("mode", ["", "default", "plan", "acceptEdits", "bypassPermissions"])
    def test_the_four_modes_and_none_are_accepted(self, mode):
        assert HITLDecision(set_permission_mode=mode).set_permission_mode == mode

    @pytest.mark.parametrize("mode", ["yolo", "acceptedits", "PLAN", "bypass"])
    def test_any_other_word_is_refused(self, mode):
        with pytest.raises(ValidationError):
            HITLDecision(set_permission_mode=mode)

    def test_no_mode_is_kept_when_none_is_named(self):
        assert HITLDecision().set_permission_mode == ""

    def test_a_response_has_no_mode_to_switch(self):
        assert "set_permission_mode" not in HITLResponse.model_fields


class TestABatchHoldsADecisionPerRequest:
    def test_an_empty_batch_has_no_decisions(self):
        assert HITLResponseBatch().decisions == []

    def test_two_batches_do_not_share_their_decisions(self):
        first = HITLResponseBatch()
        first.decisions.append(HITLDecision(requirement_id="a"))

        assert HITLResponseBatch().decisions == []

    def test_a_batch_read_from_json_holds_typed_decisions(self):
        batch = HITLResponseBatch.model_validate_json(
            '{"type": "hitl_response_batch", "decisions": ['
            '{"type": "hitl_decision", "requirement_id": "a", "action": "confirm",'
            ' "choice": "always", "set_permission_mode": "plan"}]}'
        )

        (decision,) = batch.decisions
        assert (decision.requirement_id, decision.action) == ("a", "confirm")
        assert (decision.choice, decision.set_permission_mode) == ("always", "plan")

    def test_one_bad_decision_refuses_the_whole_batch(self):
        with pytest.raises(ValidationError):
            HITLResponseBatch.model_validate({"decisions": [{"action": "approve"}]})


class TestTheChatMessages:
    def test_a_user_message_defaults_to_nothing_attached_and_no_force(self):
        message = UserMessage()

        assert message.text == ""
        assert message.file_contents == {}
        assert message.client_id == ""
        assert message.force is False

    def test_a_user_message_keeps_unicode_text_and_attached_files(self):
        message = UserMessage(
            text="héllo ☃", file_contents={"/p/é.py": "x = 1\n"}, force=True, client_id="c1"
        )

        again = UserMessage.model_validate_json(message.model_dump_json())

        assert again == message

    def test_a_user_message_does_not_share_its_file_map(self):
        UserMessage().file_contents["a"] = "b"

        assert UserMessage().file_contents == {}

    def test_a_queued_message_carries_its_text_and_sender(self):
        message = QueueMessage.model_validate({"text": "later", "client_id": "c1"})

        assert (message.type, message.text, message.client_id) == ("queue_message", "later", "c1")

    def test_a_message_of_another_type_is_refused(self):
        with pytest.raises(ValidationError):
            UserMessage.model_validate({"type": "queue_message"})


class TestTheSmallMessages:
    def test_each_has_its_type_word_and_goes_to_igni(self):
        words = {
            Command: "command",
            Cancel: "cancel",
            CancelLogin: "cancel_login",
            SessionSwitch: "session_switch",
            SessionList: "session_list",
            ModelSwitch: "model_switch",
            MCPToggle: "mcp_toggle",
            Shutdown: "shutdown",
            HITLResponse: "hitl_response",
            HITLDecision: "hitl_decision",
            HITLResponseBatch: "hitl_response_batch",
            UserMessage: "user_message",
            QueueMessage: "queue_message",
        }

        for cls, word in words.items():
            assert cls().type == word
            assert cls.direction is Direction.TO_IGNI

    def test_the_end_of_a_stream_goes_to_the_client(self):
        assert StreamEnd().type == "stream_end"
        assert StreamEnd.direction is Direction.TO_CLIENT

    def test_a_toggle_connects_unless_told_otherwise(self):
        assert MCPToggle(server_name="fs").connect is True
        assert MCPToggle(server_name="fs", connect=False).connect is False

    def test_the_named_targets_default_to_empty(self):
        assert Command().text == ""
        assert SessionSwitch().session_id == ""
        assert ModelSwitch().model_name == ""
        assert MCPToggle().server_name == ""
