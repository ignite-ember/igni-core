"""The ``Settings`` model on its own: its defaults and what its validators do.

Loading is ``SettingsLoader``'s; this is what a ``Settings`` is before any
file is read, and what constructing one always does.
"""

from __future__ import annotations

import pytest

from igni_core.core.air_gap import AIR_GAPPED_ENV
from igni_core.core.config.settings import Settings


@pytest.fixture(autouse=True)
def _no_air_gap_in_the_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(AIR_GAPPED_ENV, raising=False)


class TestDefaults:
    def test_the_product_defaults(self):
        settings = Settings()

        assert settings.api_url == ""
        assert settings.portal_url == ""
        assert settings.update_check_ttl == 86400
        assert settings.update_endpoint == ""
        assert settings.runtime_python == ""
        assert settings.air_gapped is False

    def test_defaults_and_default_dict_are_the_same_defaults(self):
        assert Settings.defaults().model_dump() == Settings.default_dict()

    def test_each_call_is_independent(self):
        first, second = Settings.defaults(), Settings.defaults()
        first.permissions.confirm_commands.append("rm")
        first.models.default = "other"
        first.api_url = "http://x"

        assert second.permissions.confirm_commands == second.safety.require_confirmation
        assert second.models.default != "other"
        assert second.api_url == ""
        d1, d2 = Settings.default_dict(), Settings.default_dict()
        d1["models"]["default"] = "mutated"
        assert d2["models"]["default"] != "mutated"

    def test_the_variable_name_is_the_air_gap_one(self):
        assert Settings.AIR_GAPPED_ENV == AIR_GAPPED_ENV == "IGNI_AIR_GAPPED"


class TestConfirmCommands:
    def test_the_safety_list_is_copied_into_permissions(self):
        settings = Settings(safety={"require_confirmation": ["git push", "rm -rf"]})

        assert settings.permissions.confirm_commands == ["git push", "rm -rf"]

    def test_it_is_a_copy_not_an_alias(self):
        settings = Settings()
        settings.permissions.confirm_commands.append("extra")

        assert "extra" not in settings.safety.require_confirmation

    def test_an_empty_list_is_carried_as_empty(self):
        assert Settings(safety={"require_confirmation": []}).permissions.confirm_commands == []

    def test_a_round_trip_carries_it_again(self):
        original = Settings(safety={"require_confirmation": ["a"]})

        again = Settings.model_validate(original.model_dump())

        assert again.permissions.confirm_commands == ["a"]

    def test_a_value_set_under_permissions_is_replaced_by_the_safety_list(self):
        settings = Settings(
            safety={"require_confirmation": ["a"]}, permissions={"confirm_commands": ["b"]}
        )

        assert settings.permissions.confirm_commands == ["a"]


class TestAirGap:
    def test_the_environment_turns_it_on_and_denies_the_web(self, monkeypatch):
        monkeypatch.setenv(AIR_GAPPED_ENV, "1")

        settings = Settings()

        assert settings.air_gapped is True
        assert settings.permissions.web_search == "deny"
        assert settings.permissions.web_fetch == "deny"

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "banana"])
    def test_an_environment_that_is_not_true_leaves_the_choice_alone(self, monkeypatch, value):
        monkeypatch.setenv(AIR_GAPPED_ENV, value)

        assert Settings().air_gapped is False
        assert Settings(air_gapped=True).air_gapped is True

    def test_a_file_that_says_air_gapped_denies_the_web_even_over_an_allow(self):
        settings = Settings(
            air_gapped=True, permissions={"web_fetch": "allow", "web_search": "allow"}
        )

        assert settings.permissions.web_fetch == "deny"
        assert settings.permissions.web_search == "deny"

    def test_not_air_gapped_leaves_the_web_levels_as_written(self):
        settings = Settings(permissions={"web_fetch": "allow", "web_search": "ask"})

        assert settings.permissions.web_fetch == "allow"
        assert settings.permissions.web_search == "ask"

    def test_validating_again_keeps_it_closed(self):
        settings = Settings(air_gapped=True)

        again = Settings.model_validate(settings.model_dump())

        assert again.air_gapped is True
        assert again.permissions.web_fetch == "deny"
