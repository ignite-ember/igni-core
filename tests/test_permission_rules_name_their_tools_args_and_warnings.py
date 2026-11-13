"""How a permission rule string is read, logged when unreadable, and matched.

Covers the rule type in ``permission_eval/schemas.py``: MCP and extension
tool names (``-``, ``:``, ``.``) are nameable, an unreadable rule is a
warning rather than a silent drop, ``file_name`` and ``url`` are argument
keys a scoped rule matches, and friendly names reach the internal tools.
"""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from igni_core.core.config.permission_eval import (
    PermissionDecision,
    PermissionEvaluator,
    PermissionMode,
    PermissionRule,
)
from igni_core.core.config.permission_eval.schemas import PermissionOutcome, _primary_arg

MCP = "mcp__chrome-devtools__take_screenshot"
EXT = "mcp_ext:acme.scanner_lookup"


@pytest.mark.parametrize("name", [MCP, EXT, "MCP:chrome-devtools"])
def test_a_tool_name_with_hyphen_colon_or_dot_parses_and_matches_itself(name: str) -> None:
    bare = PermissionRule.parse(name)
    assert bare == PermissionRule(tool=name, pattern=None)
    assert bare.matches(name, {})
    scoped = PermissionRule.parse(f"{name}(https://x.test/*)")
    assert scoped == PermissionRule(tool=name, pattern="https://x.test/*")
    assert scoped.matches(name, {"url": "https://x.test/a"})
    assert not scoped.matches(name, {"url": "https://y.test/a"})


def test_a_deny_rule_naming_an_extension_tool_denies_it() -> None:
    ev = PermissionEvaluator.from_strings(deny=[EXT], mode=PermissionMode.BYPASS_PERMISSIONS)
    assert ev.evaluate(EXT, {}) is PermissionDecision.DENY


@pytest.mark.parametrize(
    "raw", ["", "   ", "garbage(", "1abc", "Bash(x", "**", "*x", "-a", "Bash (x)"]
)
def test_unreadable_rule_strings_parse_to_none(raw: str) -> None:
    assert PermissionRule.parse(raw) is None


def test_the_pattern_is_kept_verbatim_and_greedy_over_parens() -> None:
    assert PermissionRule.parse("Bash(echo (a) (b))").pattern == "echo (a) (b)"
    assert PermissionRule.parse("  Bash(rm *)  ") == PermissionRule(tool="Bash", pattern="rm *")
    assert PermissionRule.parse("Bash()").pattern == ""


def test_display_and_deny_reason_show_the_rule_as_written() -> None:
    assert PermissionRule(tool="Bash", pattern="rm *").display() == "Bash(rm *)"
    assert PermissionRule(tool="Bash").display() == "Bash"
    assert PermissionRule(tool="Bash", pattern="").display() == "Bash"
    assert (
        PermissionRule(tool="Bash", pattern="rm *").deny_reason()
        == "deny rule 'Bash(rm *)' matched"
    )
    assert PermissionRule(tool="*").deny_reason() == "deny rule '*' matched"


def test_parse_many_warns_naming_each_dropped_entry(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        rules = PermissionRule.parse_many(["Bash", "garbage(", "ok_tool(x)", "9bad"])
    assert [r.tool for r in rules] == ["Bash", "ok_tool"]
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 2
    assert "'garbage('" in warned[0] and "ignored" in warned[0]
    assert "'9bad'" in warned[1]


def test_parse_many_is_silent_when_every_rule_parses(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert len(PermissionRule.parse_many(["Bash", "Read(./x)"])) == 2
    assert not caplog.records


def test_a_scoped_write_rule_matches_save_file_by_its_file_name() -> None:
    rule = PermissionRule.parse("Write(.env)")
    assert rule.matches("save_file", {"file_name": ".env", "contents": "x"})
    assert not rule.matches("save_file", {"file_name": "notes.txt"})
    ev = PermissionEvaluator.from_strings(
        deny=["Write(.env)"], mode=PermissionMode.BYPASS_PERMISSIONS
    )
    assert ev.evaluate("save_file", {"file_name": ".env"}) is PermissionDecision.DENY
    assert ev.evaluate("save_file", {"file_name": "ok.txt"}) is PermissionDecision.ALLOW


def test_the_primary_argument_is_picked_in_priority_order_and_skips_empty_or_non_str() -> None:
    full = {
        "url": "u",
        "filename": "fn",
        "path": "p",
        "file_name": "f_n",
        "file_path": "fp",
        "command": "c",
    }
    order = ["command", "file_path", "file_name", "path", "filename", "url"]
    expect = ["c", "fp", "f_n", "p", "fn", "u"]
    args = dict(full)
    for key, value in zip(order, expect, strict=True):
        assert _primary_arg("t", args) == value
        del args[key]
    assert _primary_arg("t", args) is None
    assert _primary_arg("t", {"command": "", "file_path": 3, "path": None, "url": "u"}) == "u"


def test_the_args_list_is_joined_only_when_no_key_is_present() -> None:
    assert _primary_arg("t", {"args": ["rm", "-rf", 3]}) == "rm -rf 3"
    assert _primary_arg("t", {"args": []}) is None
    assert _primary_arg("t", {"args": "rm"}) is None
    assert _primary_arg("t", {"args": ["x"], "command": "c"}) == "c"
    assert PermissionRule.parse("Bash(rm *)").matches("Bash", {"args": ["rm", "-rf", "b"]})


def test_a_pattern_needs_a_target_and_is_case_sensitive_glob() -> None:
    rule = PermissionRule.parse("Read(./Secret*)")
    assert rule.matches("Read", {"path": "./Secret.txt"})
    assert not rule.matches("Read", {"path": "./secret.txt"})
    assert not rule.matches("Read", {})
    assert not PermissionRule.parse("Read()").matches("Read", {})


def test_a_wildcard_rule_matches_any_tool_and_a_bare_rule_any_args() -> None:
    assert PermissionRule.parse("*").matches("whatever", {"command": "x"})
    assert PermissionRule.parse("Bash").matches("Bash", {"command": "anything"})
    assert not PermissionRule.parse("Bash").matches("Read", {})


def test_a_friendly_bash_rule_fires_on_the_internal_shell_tool() -> None:
    rule = PermissionRule.parse("Bash(rm *)")
    assert rule.matches("run_shell_command", {"command": "rm -rf x"})
    assert not rule.matches("run_shell_command", {"command": "ls"})
    ev = PermissionEvaluator.from_strings(
        deny=["Bash(rm *)"], mode=PermissionMode.BYPASS_PERMISSIONS
    )
    assert ev.evaluate("run_shell_command", {"command": "rm -rf x"}) is PermissionDecision.DENY


def test_a_friendly_rule_does_not_fire_on_an_unrelated_internal_tool() -> None:
    assert not PermissionRule.parse("Bash").matches("read_file", {})
    assert PermissionRule.parse("Bash(rm *)").matches("run_shell_command", {"command": "rm a"})


def test_an_outcome_cannot_be_edited_and_refuses_an_unknown_source() -> None:
    out = PermissionOutcome(decision=PermissionDecision.DENY, reason="r", source="mode")
    with pytest.raises(ValidationError):
        out.reason = "other"
    with pytest.raises(ValidationError):
        PermissionOutcome(decision=PermissionDecision.DENY, reason="r", source="nope")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        PermissionRule(tool="x").tool = "y"  # type: ignore[misc]


def test_decisions_are_four_distinct_members() -> None:
    assert {d.value for d in PermissionDecision} == {"allow", "deny", "ask", "defer"}
