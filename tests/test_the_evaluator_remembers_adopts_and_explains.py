"""The evaluator's live rule lists, its settings reload and its explanations.

``remember`` and ``remember_at`` append to the lists the hook pipeline holds,
once, and take effect on the next call. ``adopt`` swaps in a reloaded
settings block without moving the person's mode or loosening ``--read-only``.
Where a mode never prompts, an ask becomes a deny, also under ``--read-only``.
"""

from __future__ import annotations

import pytest

from igni_core.core.config.permission_eval import (
    PermissionDecision,
    PermissionMode,
    PermissionRule,
)
from igni_core.core.config.permission_eval.pipeline import PermissionEvaluator
from igni_core.core.config.schemas.permissions import PermissionsConfig
from igni_core.core.mcp.hints import ToolHints, ToolHintsRegistry

SHELL = "run_shell_command"


def _rule(text: str) -> PermissionRule:
    rule = PermissionRule.parse(text)
    assert rule is not None
    return rule


def _run(evaluator: PermissionEvaluator, command: str) -> PermissionDecision:
    return evaluator.evaluate(SHELL, {"command": command})


def test_a_remembered_allow_takes_effect_on_the_next_call_and_is_added_once():
    evaluator = PermissionEvaluator.from_strings()
    assert _run(evaluator, "make test") is PermissionDecision.DEFER
    rule = _rule("Bash(make *)")
    evaluator.remember(rule, deny=False)
    evaluator.remember(_rule("Bash(make *)"), deny=False)
    assert evaluator.allow == [rule]
    assert _run(evaluator, "make test") is PermissionDecision.ALLOW


def test_a_remembered_deny_goes_to_the_deny_list_and_wins():
    evaluator = PermissionEvaluator.from_strings(allow=["Bash(make *)"])
    evaluator.remember(_rule("Bash(make clean)"), deny=True)
    assert [r.display() for r in evaluator.deny] == ["Bash(make clean)"]
    assert evaluator.allow != []
    assert _run(evaluator, "make clean") is PermissionDecision.DENY
    assert _run(evaluator, "make test") is PermissionDecision.ALLOW


@pytest.mark.parametrize("level", ["allow", "ask", "deny"])
def test_a_rule_remembered_at_a_level_lands_only_there(level: str):
    evaluator = PermissionEvaluator.from_strings()
    rule = _rule("Bash(make *)")
    evaluator.remember_at(rule, level)  # type: ignore[arg-type]
    evaluator.remember_at(_rule("Bash(make *)"), level)  # type: ignore[arg-type]
    lists = {"allow": evaluator.allow, "ask": evaluator.ask, "deny": evaluator.deny}
    for name, rules in lists.items():
        assert rules == ([rule] if name == level else [])


def test_remembering_appends_in_place_to_the_held_lists():
    evaluator = PermissionEvaluator.from_strings()
    held = evaluator.ask
    evaluator.remember_at(_rule("Bash(make *)"), "ask")
    assert held is evaluator.ask
    assert len(held) == 1


def test_adopt_replaces_the_rules_in_place():
    evaluator = PermissionEvaluator.from_strings(allow=["Bash(ls *)"], deny=["Bash(rm *)"])
    held = (evaluator.allow, evaluator.ask, evaluator.deny)
    fresh = PermissionEvaluator.from_strings(ask=["Bash(git *)"], allow=["Bash(cat *)"])
    evaluator.adopt(fresh, previous_default="default")
    assert (evaluator.allow, evaluator.ask, evaluator.deny) == held
    assert [r.display() for r in evaluator.allow] == ["Bash(cat *)"]
    assert [r.display() for r in evaluator.ask] == ["Bash(git *)"]
    assert evaluator.deny == []
    assert _run(evaluator, "git status") is PermissionDecision.ASK


def test_adopt_never_loosens_read_only_but_can_tighten_it():
    loose = PermissionEvaluator.from_strings()
    tight = PermissionEvaluator.from_strings()
    tight.read_only = True
    tight.adopt(loose, previous_default="default")
    assert tight.read_only is True
    loose.adopt(tight, previous_default="default")
    assert loose.read_only is True


def test_adopt_follows_a_new_default_mode_only_if_the_person_never_left_the_old_one():
    fresh = PermissionEvaluator.from_strings(mode="acceptEdits")

    stayed = PermissionEvaluator.from_strings(mode="default")
    stayed.adopt(fresh, previous_default="default")
    assert stayed.mode is PermissionMode.ACCEPT_EDITS

    moved = PermissionEvaluator.from_strings(mode="default")
    moved.set_mode(PermissionMode.PLAN)
    moved.adopt(fresh, previous_default="default")
    assert moved.mode is PermissionMode.PLAN

    unchanged = PermissionEvaluator.from_strings(mode="default")
    unchanged.adopt(PermissionEvaluator.from_strings(mode="default"), previous_default="default")
    assert unchanged.mode is PermissionMode.DEFAULT


def test_from_strings_takes_a_mode_string_or_the_enum_and_refuses_an_unknown_one():
    assert PermissionEvaluator.from_strings(mode="plan").mode is PermissionMode.PLAN
    assert (
        PermissionEvaluator.from_strings(mode=PermissionMode.DONT_ASK).mode
        is PermissionMode.DONT_ASK
    )
    with pytest.raises(ValueError):
        PermissionEvaluator.from_strings(mode="nonsense")


def test_from_permissions_puts_category_rules_behind_the_explicit_ones_and_carries_read_only():
    perms = PermissionsConfig(allow=["Bash(ls *)"], shell_execute="deny", read_only=True)
    evaluator = PermissionEvaluator.from_permissions(perms)
    assert evaluator.read_only is True
    assert evaluator.allow[0].display() == "Bash(ls *)"
    assert _run(evaluator, "ls") is PermissionDecision.DENY


def test_explain_deny_gives_the_generic_line_when_the_call_is_not_denied():
    evaluator = PermissionEvaluator.from_strings(allow=["Bash(ls *)"])
    assert evaluator.explain_deny(SHELL, {"command": "ls x"}) == f"{SHELL} is denied by policy"
    assert evaluator.explain_deny(SHELL, {"command": "pwd"}) == f"{SHELL} is denied by policy"


def test_explain_deny_gives_the_reason_the_deny_came_with():
    evaluator = PermissionEvaluator.from_strings(deny=["Bash(rm *)"])
    outcome = evaluator.evaluate_outcome(SHELL, {"command": "cd x && rm -rf ."})
    assert outcome.decision is PermissionDecision.DENY
    assert evaluator.explain_deny(SHELL, {"command": "cd x && rm -rf ."}) == outcome.reason


def test_an_ask_rule_becomes_a_deny_where_the_mode_never_prompts():
    evaluator = PermissionEvaluator.from_strings(mode="dontAsk", ask=["Bash(git push *)"])
    outcome = evaluator.evaluate_outcome(SHELL, {"command": "git push --force"})
    assert outcome.decision is PermissionDecision.DENY
    assert outcome.source == "ask"
    assert "never prompts" in outcome.reason
    assert "ask rule 'Bash(git push *)' matched" in outcome.reason


def test_an_ask_rule_still_asks_where_the_mode_prompts():
    evaluator = PermissionEvaluator.from_strings(ask=["Bash(git push *)"])
    outcome = evaluator.evaluate_outcome(SHELL, {"command": "git push --force"})
    assert outcome.decision is PermissionDecision.ASK
    assert "never prompts" not in outcome.reason


def test_in_read_only_an_ask_rule_on_a_read_is_denied_where_the_mode_never_prompts():
    evaluator = PermissionEvaluator.from_strings(mode="dontAsk", ask=["Bash(cat *)"])
    evaluator.read_only = True
    outcome = evaluator.evaluate_outcome(SHELL, {"command": "cat x"})
    assert outcome.decision is PermissionDecision.DENY
    assert "never prompts" in outcome.reason


def test_in_read_only_a_flagged_url_is_denied_where_the_mode_never_prompts():
    evaluator = PermissionEvaluator.from_strings(mode="dontAsk")
    evaluator.read_only = True
    outcome = evaluator.evaluate_outcome(
        "fetch_url", {"url": "https://x.test/?api_key=sk-abcdef123456"}
    )
    assert outcome.decision is PermissionDecision.DENY
    assert "never prompts" in outcome.reason


def test_in_read_only_a_fetch_without_a_url_is_not_flagged():
    evaluator = PermissionEvaluator.from_strings()
    evaluator.read_only = True
    for args in ({}, {"url": None}):
        assert evaluator.evaluate("fetch_url", args) is PermissionDecision.ALLOW


def test_in_read_only_a_blank_shell_command_falls_through_to_the_mode():
    evaluator = PermissionEvaluator.from_strings()
    evaluator.read_only = True
    assert _run(evaluator, "   ") is PermissionDecision.DEFER
    evaluator.set_mode(PermissionMode.BYPASS_PERMISSIONS)
    assert _run(evaluator, "") is PermissionDecision.ALLOW


def test_in_read_only_a_tool_that_changes_something_is_refused_by_name():
    registry = ToolHintsRegistry.shared()
    registry.declare(
        "ext:acme.rw",
        {
            "mcp_ext:acme.rebuild": ToolHints(
                read_only=False, destructive=True, idempotent=True, open_world=False
            )
        },
    )
    try:
        evaluator = PermissionEvaluator.from_strings()
        evaluator.read_only = True
        outcome = evaluator.evaluate_outcome("mcp_ext:acme.rebuild", {})
    finally:
        registry.forget("ext:acme.rw")
    assert outcome.decision is PermissionDecision.DENY
    assert outcome.source == "mode"
    assert outcome.reason == (
        "this session is read-only (--read-only): mcp_ext:acme.rebuild can change "
        "something, so it does not run. Say what you would use it for rather than retrying."
    )


def test_in_read_only_a_tool_with_no_hints_is_left_to_the_mode():
    evaluator = PermissionEvaluator.from_strings()
    evaluator.read_only = True
    assert evaluator.evaluate("some_internal_planning_tool", {}) is PermissionDecision.DEFER
    evaluator.set_mode(PermissionMode.BYPASS_PERMISSIONS)
    assert evaluator.evaluate("some_internal_planning_tool", {}) is PermissionDecision.ALLOW
