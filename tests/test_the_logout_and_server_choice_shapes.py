"""The auth schemas: what ``/logout`` says, and the shapes of the server RPCs.

``/logout`` prints an identity line, then either the model it fell back to or
a warning that none was available. The server-choice models carry a refusal
or a discovery as plain fields the SDKs generate from.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.backend.schemas_auth import (
    CloudSwitchOutcome,
    DiscoverServerArgs,
    LogoutOutcome,
    ServerChoice,
    ServerFound,
    SetServerArgs,
)
from igni_core.core.auth.discovery_schemas import Discovered
from igni_core.protocol.messages import CommandAction, CommandResultKind


class TestLogoutOutcome:
    def test_identity_alone_is_one_line(self):
        result = LogoutOutcome(identity_message="Logged out (ada@example.com).").to_command_result()

        assert result.content == "Logged out (ada@example.com)."
        assert result.kind == CommandResultKind.INFO
        assert result.action == CommandAction.LOGOUT

    def test_a_fallback_is_the_second_line(self):
        result = LogoutOutcome(
            identity_message="Logged out (a@b.c).", fallback_model="local-7b"
        ).to_command_result()

        assert result.content == (
            "Logged out (a@b.c).\nSwitched to local-7b (cloud model no longer available)."
        )

    def test_a_warning_is_the_second_line_when_there_is_no_fallback(self):
        result = LogoutOutcome(
            identity_message="Not logged in.", warning="No local model is configured."
        ).to_command_result()

        assert result.content == "Not logged in.\nNo local model is configured."

    def test_the_fallback_wins_over_a_warning(self):
        result = LogoutOutcome(
            identity_message="Logged out (a@b.c).", fallback_model="local-7b", warning="ignored"
        ).to_command_result()

        assert "ignored" not in result.content
        assert "Switched to local-7b" in result.content

    def test_an_empty_identity_message_still_leads(self):
        result = LogoutOutcome(identity_message="", warning="w").to_command_result()

        assert result.content == "\nw"

    def test_the_identity_message_is_required(self):
        with pytest.raises(ValidationError):
            LogoutOutcome()  # type: ignore[call-arg]


class TestCloudSwitchOutcome:
    def test_no_switch_needed_sets_nothing(self):
        outcome = CloudSwitchOutcome.no_switch_needed()

        assert (outcome.fallback_model, outcome.warning, outcome.switched) == (None, None, False)

    def test_switched_to_names_the_fallback(self):
        outcome = CloudSwitchOutcome.switched_to("local-7b")

        assert (outcome.fallback_model, outcome.warning, outcome.switched) == (
            "local-7b",
            None,
            True,
        )

    def test_cloud_but_no_fallback_carries_only_the_warning(self):
        outcome = CloudSwitchOutcome.cloud_but_no_fallback("none left")

        assert (outcome.fallback_model, outcome.warning, outcome.switched) == (
            None,
            "none left",
            False,
        )


class TestServerModels:
    def test_a_discovery_is_copied_field_for_field(self):
        found = ServerFound.from_discovered(
            Discovered(
                api_url="https://api.example.com",
                portal_url="https://portal.example.com",
                source="dns",
                reason="",
            )
        )

        assert found.model_dump() == {
            "api_url": "https://api.example.com",
            "portal_url": "https://portal.example.com",
            "source": "dns",
            "reason": "",
        }

    def test_a_discovery_that_found_nothing_keeps_its_reason(self):
        found = ServerFound.from_discovered(Discovered(reason="no record"))

        assert found.model_dump() == {
            "api_url": "",
            "portal_url": "",
            "source": "",
            "reason": "no record",
        }

    def test_a_refused_choice_has_empty_urls_and_the_error(self):
        choice = ServerChoice.refused("not an igni server")

        assert choice.model_dump() == {
            "api_url": "",
            "portal_url": "",
            "error": "not an igni server",
        }

    def test_set_server_derives_the_portal_when_none_is_given(self):
        assert SetServerArgs(api_url="https://api.example.com").portal_url == ""

    def test_discover_server_needs_an_email(self):
        with pytest.raises(ValidationError):
            DiscoverServerArgs()  # type: ignore[call-arg]

    def test_a_server_url_is_not_validated_here(self):
        assert SetServerArgs(api_url="not a url").api_url == "not a url"
