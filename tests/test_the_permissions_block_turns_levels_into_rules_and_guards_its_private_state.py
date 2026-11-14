"""``permissions``: defaults, the level-to-rule translation and what no file can write."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.core.config.permission_eval.schemas import PermissionMode
from igni_core.core.config.schemas import PermissionsConfig
from igni_core.core.config.schemas.permissions import CategoryRules


def test_defaults_ask_for_writes_shell_web_and_git_and_allow_reads():
    config = PermissionsConfig()
    assert config.file_read == "allow"
    assert config.shell_restricted == "allow"
    for category in ("file_write", "shell_execute", "web_search", "web_fetch"):
        assert getattr(config, category) == "ask", category
    assert config.git_push == "ask"
    assert config.git_destructive == "ask"
    assert config.allow_bypass is True
    assert config.read_only is False
    assert (config.deny, config.ask, config.allow) == ([], [], [])


def test_the_mode_is_stored_as_its_plain_string_even_when_assigned_as_the_enum():
    config = PermissionsConfig()
    assert config.mode == "default"
    assert type(config.mode) is str
    config.mode = PermissionMode.PLAN
    assert config.mode == "plan"
    assert type(config.mode) is str


def test_an_invalid_level_or_mode_is_refused_on_construction_and_on_assignment():
    config = PermissionsConfig()
    with pytest.raises(ValidationError):
        PermissionsConfig(file_write="sometimes")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        config.git_push = "maybe"  # type: ignore[assignment]
    with pytest.raises(ValidationError):
        config.mode = "anything"  # type: ignore[assignment]
    assert config.git_push == "ask"


def test_a_default_config_emits_no_category_rules():
    rules = PermissionsConfig().category_rules()
    assert (rules.deny, rules.ask, rules.allow) == ([], [], [])


def test_a_category_set_off_its_default_lands_in_the_list_for_its_level():
    config = PermissionsConfig(file_write="deny", web_fetch="allow")
    rules = config.category_rules()
    assert rules.deny == list(PermissionsConfig.CATEGORY_FUNCTIONS["file_write"])
    assert rules.allow == ["fetch_url", "fetch_json"]
    assert rules.ask == []


def test_read_and_restricted_shell_levels_never_emit_rules():
    config = PermissionsConfig(file_read="deny", shell_restricted="deny")
    assert "file_read" not in PermissionsConfig.CATEGORY_FUNCTIONS
    assert "shell_restricted" not in PermissionsConfig.CATEGORY_FUNCTIONS
    rules = config.category_rules()
    assert (rules.deny, rules.ask, rules.allow) == ([], [], [])


def test_git_categories_match_a_command_on_its_own_and_after_a_separator():
    rules = PermissionsConfig(git_push="deny").category_rules()
    assert rules.deny == [
        "run_shell_command(git push*)",
        "run_shell_command(* git push*)",
    ]


def test_a_final_field_is_emitted_even_at_its_default():
    config = PermissionsConfig()
    config.set_final_fields(frozenset({"shell_execute"}))
    rules = config.category_rules()
    assert rules.ask == ["run_shell_command"]
    assert rules.deny == [] and rules.allow == []


def test_final_fields_and_mode_policy_round_trip_through_their_setters_only():
    config = PermissionsConfig()
    assert config.final_fields == frozenset()
    assert config.mode_policy is None
    config.set_final_fields(frozenset({"git_push"}))
    sentinel = object()
    config.set_mode_policy(sentinel)  # type: ignore[arg-type]
    assert config.final_fields == frozenset({"git_push"})
    assert config.mode_policy is sentinel
    with pytest.raises(AttributeError):
        config.final_fields = frozenset()  # type: ignore[misc]


def test_a_config_file_cannot_write_final_fields_or_the_mode_policy_and_neither_is_dumped():
    config = PermissionsConfig.model_validate(
        {"_final_fields": ["git_push"], "_mode_policy": {"x": 1}, "final_fields": ["git_push"]}
    )
    assert config.final_fields == frozenset()
    assert config.mode_policy is None
    config.set_final_fields(frozenset({"git_push"}))
    dumped = config.model_dump()
    assert not {"final_fields", "_final_fields", "mode_policy", "_mode_policy"} & set(dumped)


def test_confirmation_commands_become_ask_rules_but_are_never_serialised():
    config = PermissionsConfig(confirm_commands=["terraform apply"])
    assert config.category_rules().ask == [
        "run_shell_command(terraform apply*)",
        "run_shell_command(* terraform apply*)",
    ]
    assert "confirm_commands" not in config.model_dump()
    assert "confirm_commands" not in config.model_dump_json()


def test_confirmation_rules_follow_the_categories_in_the_ask_list():
    config = PermissionsConfig(shell_execute="ask", confirm_commands=["x"])
    config.set_final_fields(frozenset({"shell_execute"}))
    assert config.category_rules().ask == [
        "run_shell_command",
        "run_shell_command(x*)",
        "run_shell_command(* x*)",
    ]


def test_dont_ask_mode_drops_the_confirmation_rules_and_keeps_the_categories():
    config = PermissionsConfig(
        mode="dontAsk", confirm_commands=["terraform apply"], file_write="deny"
    )
    rules = config.category_rules()
    assert rules.ask == []
    assert rules.deny == list(PermissionsConfig.CATEGORY_FUNCTIONS["file_write"])


def test_no_confirmation_commands_give_no_ask_rules():
    assert PermissionsConfig(confirm_commands=[]).category_rules().ask == []


def test_adding_rules_for_an_unknown_level_drops_them():
    rules = CategoryRules()
    rules.add("deny", ["a"])
    rules.add("ask", ["b"])
    rules.add("allow", ["c"])
    rules.add("bogus", ["d"])  # type: ignore[arg-type]
    assert (rules.deny, rules.ask, rules.allow) == (["a"], ["b"], ["c"])
