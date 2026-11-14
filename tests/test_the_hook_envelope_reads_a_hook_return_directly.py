"""``HookEnvelope`` itself: what it makes of a raw hook return, and what it hands back.

``test_hook_envelope_parser.py`` drives the executor's wrapper; these
call the envelope model directly, so the None-for-non-dict contract,
the alias handling and the permission precedence are pinned on the
class that owns them.
"""

from __future__ import annotations

import pytest

from igni_core.core.hooks.envelope import HookEnvelope
from igni_core.core.hooks.schemas import HookResult, PermissionDecision


@pytest.mark.parametrize("raw", ["allow", None, ["continue"], 3, b"{}"])
def test_a_return_that_is_not_a_dict_gives_no_envelope(raw) -> None:
    assert HookEnvelope.from_raw(raw) is None


def test_an_empty_dict_continues_silently_with_no_opinion() -> None:
    result = HookEnvelope.from_raw({}).to_result()
    assert result == HookResult(
        should_continue=True, message="", permission_decision=PermissionDecision.NONE
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [(False, False), (0, False), (1, True), (True, True), (None, True), ("", False)],
)
def test_continue_is_coerced_to_a_bool_and_none_means_continue(value, expected) -> None:
    assert HookEnvelope.from_raw({"continue": value}).should_continue is expected


def test_the_snake_case_field_names_are_accepted_as_well_as_the_wire_aliases() -> None:
    by_name = HookEnvelope.from_raw({"should_continue": False, "system_message": "stop"})
    by_alias = HookEnvelope.from_raw({"continue": False, "systemMessage": "stop"})
    assert by_name.to_result() == by_alias.to_result()
    assert by_name.should_continue is False
    assert by_name.system_message == "stop"


@pytest.mark.parametrize(("value", "expected"), [(None, ""), (42, "42"), ("blocked", "blocked")])
def test_a_system_message_is_text_and_none_is_never_the_word_none(value, expected) -> None:
    assert HookEnvelope.from_raw({"systemMessage": value}).to_result().message == expected


@pytest.mark.parametrize("garbage", ["allow", ["deny"], None, 7])
def test_a_hook_specific_output_that_is_not_a_dict_is_treated_as_empty(garbage) -> None:
    envelope = HookEnvelope.from_raw({"hookSpecificOutput": garbage, "permissionDecision": "deny"})
    assert envelope.hook_specific_output == {}
    assert envelope.permission_decision is PermissionDecision.DENY


def test_the_nested_decision_wins_over_the_bare_one() -> None:
    envelope = HookEnvelope.from_raw(
        {"hookSpecificOutput": {"permissionDecision": "allow"}, "permissionDecision": "deny"}
    )
    assert envelope.permission_decision is PermissionDecision.ALLOW


@pytest.mark.parametrize("nested", ["", None])
def test_an_empty_nested_decision_falls_back_to_the_bare_one(nested) -> None:
    envelope = HookEnvelope.from_raw(
        {"hookSpecificOutput": {"permissionDecision": nested}, "permissionDecision": "ask"}
    )
    assert envelope.permission_decision is PermissionDecision.ASK


def test_a_none_bare_decision_is_no_decision() -> None:
    assert HookEnvelope.from_raw({"permissionDecision": None}).permission_decision is (
        PermissionDecision.NONE
    )


@pytest.mark.parametrize("garbage", ["ALLOW", "maybe", 5])
def test_an_unrecognised_decision_is_no_decision(garbage) -> None:
    envelope = HookEnvelope.from_raw({"permissionDecision": garbage})
    assert envelope.permission_decision is PermissionDecision.NONE
    assert envelope.to_result().permission_decision is PermissionDecision.NONE


def test_unknown_keys_are_allowed_and_do_not_change_the_result() -> None:
    plain = HookEnvelope.from_raw({"continue": False}).to_result()
    extra = HookEnvelope.from_raw({"continue": False, "suppressOutput": True, "x": [1]}).to_result()
    assert extra == plain
