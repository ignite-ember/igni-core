"""An "always allow" rule says what was approved: this call, or calls like it.

When the person picks "Allow" or "Allow similar" in the approval dialog,
:class:`ToolInvocation` writes the rule that is persisted. "Allow" must
name this exact call (``Bash(python3 -m http.server 8000)``) so it does
not widen; "Allow similar" widens by one deliberate step — the binary, the
parent directory, the domain — and to nothing wider when there is no such
step. Rules written by the earlier free functions must still round-trip
byte for byte, so the shapes below are the wire format, not a detail.
"""

from __future__ import annotations

import pytest

from igni_core.core.config.tool_permissions.schemas import PermissionRule
from igni_core.core.config.tool_permissions.tool_invocation import ToolInvocation


@pytest.mark.parametrize(
    ("tool", "args", "exact", "similar"),
    [
        (
            "Bash",
            {"args": ["python3", "-m", "http.server", "8000"]},
            "Bash(python3 -m http.server 8000)",
            "Bash(python3 *)",
        ),
        ("Bash", {"command": "  npm run build  "}, "Bash(  npm run build  )", "Bash(npm *)"),
        ("Edit", {"path": "src/app/main.py"}, "Edit(src/app/main.py)", "Edit(src/app/*)"),
        ("Write", {"file_path": "docs/a.md"}, "Write(docs/a.md)", "Write(docs/*)"),
        (
            "WebFetch",
            {"url": "https://example.org/a/b?c=1"},
            "WebFetch(https://example.org/a/b?c=1)",
            "WebFetch(domain:example.org)",
        ),
        ("WebSearch", {"query": "pydantic v2"}, "WebSearch(pydantic v2)", "WebSearch"),
    ],
)
def test_allow_names_the_call_and_allow_similar_widens_one_step(tool, args, exact, similar):
    invocation = ToolInvocation.from_raw(tool, args)

    assert invocation.exact_rule() == exact
    assert invocation.pattern_rule() == similar


def test_a_call_with_no_arguments_is_allowed_by_its_bare_name():
    for raw in (None, {}):
        invocation = ToolInvocation.from_raw("TodoWrite", raw)

        assert invocation.exact_rule() == "TodoWrite"
        assert invocation.pattern_rule() == "TodoWrite"


def test_a_file_at_the_top_level_does_not_widen_to_the_whole_project():
    invocation = ToolInvocation.from_raw("Edit", {"path": "README.md"})

    assert invocation.pattern_rule() == "Edit"


def test_a_blank_command_widens_to_nothing():
    invocation = ToolInvocation.from_raw("Bash", {"command": "   "})

    assert invocation.pattern_rule() == "Bash"


def test_a_url_without_a_host_widens_to_nothing():
    invocation = ToolInvocation.from_raw("WebFetch", {"url": "not-a-url"})

    assert invocation.pattern_rule() == "WebFetch"


def test_unrecognised_arguments_are_named_and_cut_to_a_hundred_characters():
    invocation = ToolInvocation.from_raw("Custom", {"payload": "x" * 300})

    rule = invocation.exact_rule()

    assert rule.startswith("Custom({'payload': 'xxx")
    assert len(rule) == len("Custom()") + 100
    assert invocation.pattern_rule() == "Custom"


def test_a_file_at_the_root_widens_to_the_root_not_to_a_pattern_that_matches_nothing():
    invocation = ToolInvocation.from_raw("Write", {"path": "/notes.txt"})

    similar = invocation.pattern_rule()

    assert similar == "Write(/*)"
    rule = PermissionRule.parse(similar, level="allow")
    assert rule is not None and rule.matches("Write", invocation.args)


def test_a_nested_absolute_path_widens_to_its_directory():
    assert (
        ToolInvocation.from_raw("Edit", {"file_path": "/a/b/c.py"}).pattern_rule() == "Edit(/a/b/*)"
    )


def test_the_argument_list_outranks_the_command_which_outranks_the_path_and_the_url():
    both = {
        "args": ["git", "log"],
        "command": "npm test",
        "path": "src/a.py",
        "url": "https://x.test",
    }
    invocation = ToolInvocation.from_raw("Bash", both)
    assert invocation.exact_rule() == "Bash(git log)"
    assert invocation.pattern_rule() == "Bash(git *)"

    del both["args"]
    invocation = ToolInvocation.from_raw("Bash", both)
    assert invocation.exact_rule() == "Bash(npm test)"
    assert invocation.pattern_rule() == "Bash(npm *)"

    del both["command"]
    invocation = ToolInvocation.from_raw("Bash", both)
    assert invocation.exact_rule() == "Bash(src/a.py)"
    assert invocation.pattern_rule() == "Bash(src/*)"

    del both["path"]
    invocation = ToolInvocation.from_raw("Bash", both)
    assert invocation.exact_rule() == "Bash(https://x.test)"
    assert invocation.pattern_rule() == "Bash(domain:x.test)"


def test_a_file_name_is_not_named_in_the_exact_rule_and_does_not_widen():
    invocation = ToolInvocation.from_raw("Read", {"file_name": "a.txt"})

    assert invocation.exact_rule() == "Read({'file_name': 'a.txt'})"
    assert invocation.pattern_rule() == "Read"


def test_an_argument_list_with_numbers_is_named_by_their_text():
    invocation = ToolInvocation.from_raw("Bash", {"args": ["sleep", 5]})

    assert invocation.exact_rule() == "Bash(sleep 5)"
    assert invocation.pattern_rule() == "Bash(sleep *)"


def test_a_command_is_widened_by_its_first_word_whatever_follows_it():
    invocation = ToolInvocation.from_raw("Bash", {"command": "\n  make  all && rm -rf x; ls"})

    assert invocation.pattern_rule() == "Bash(make *)"


@pytest.mark.parametrize(
    "raw",
    [
        {"args": ["python3", "-m", "http.server", "8000"]},
        {"command": "npm run build"},
        {"command": "echo a:b"},
        {"path": "src/app/main.py"},
        {"path": "/notes.txt"},
        {"url": "https://example.org/a/b?c=1"},
        {"query": "pydantic v2"},
    ],
)
def test_both_rules_a_call_produces_match_the_call_they_came_from(raw):
    """What the person approved must cover what they approved: the rule
    persisted by "Allow" and by "Allow similar" is read back by the same
    matchers that will meet the next call."""
    invocation = ToolInvocation.from_raw("Tool", raw)

    for text in (invocation.exact_rule(), invocation.pattern_rule()):
        rule = PermissionRule.parse(text, level="allow")
        assert rule is not None, text
        assert rule.matches("Tool", invocation.args), text
