"""HookMatcher read directly: always, exact or pipe list, regex search, and a bad regex."""

from __future__ import annotations

import pytest

from igni_core.core.hooks.matcher import HookMatcher


@pytest.mark.parametrize("pattern", ["", "*"])
@pytest.mark.parametrize("target", ["Edit", "", "anything at all"])
def test_an_empty_or_star_pattern_matches_every_target(pattern, target):
    assert HookMatcher(pattern).matches(target) is True


def test_always_matches_every_target():
    matcher = HookMatcher.always()

    assert matcher.matches("Edit") is True
    assert matcher.matches("") is True


@pytest.mark.parametrize(
    ("pattern", "target", "expected"),
    [
        ("Edit", "Edit", True),
        ("Edit", "Edited", False),
        ("Edit", "MultiEdit", False),
        ("Edit", "edit", False),
        ("Edit|Write", "Write", True),
        ("Edit|Write", "Edit", True),
        ("Edit|Write", "Edit|Write", False),
        ("Edit|Write", "Read", False),
        ("Edit|Write", "", False),
        ("mcp_server_tool", "mcp_server_tool", True),
        ("_private1", "_private1", True),
    ],
)
def test_an_identifier_or_pipe_list_matches_exactly_and_case_sensitively(pattern, target, expected):
    assert HookMatcher(pattern).matches(target) is expected


@pytest.mark.parametrize(
    ("pattern", "target", "expected"),
    [
        ("^Edit", "EditFile", True),
        ("^Edit", "MultiEdit", False),
        ("^Edit$", "EditFile", False),
        ("mcp__.*__read", "mcp__files__read", True),
        ("mcp__.*__read", "xx mcp__files__read yy", True),
        ("Edit.*", "edit_file", False),
        ("mcp-x", "use mcp-x here", True),
        ("1abc", "x1abcx", True),
        ("héllo", "say héllo", True),
    ],
)
def test_any_other_pattern_is_a_case_sensitive_regex_searched_in_the_target(
    pattern, target, expected
):
    assert HookMatcher(pattern).matches(target) is expected


@pytest.mark.parametrize("pattern", ["[", "(", "*abc", "a{2,1}", "(?P<x"])
@pytest.mark.parametrize("target", ["[", "(", "abc", ""])
def test_a_malformed_regex_matches_nothing_and_never_raises(pattern, target):
    assert HookMatcher(pattern).matches(target) is False
