"""What a permission rule is matched against, and which calls each kind of rule covers.

The argument side of a rule is a family of patterns (bare, domain, path,
key/value, glob), and the arguments they read are whatever the model sent.
A call with an odd argument must be matched, not crash the permission check.
"""

from __future__ import annotations

import pytest

from igni_core.core.config.permissions.schemas import PermissionCategory
from igni_core.core.config.schemas import PermissionsConfig
from igni_core.core.config.tool_permissions import (
    FUNC_TO_TOOL,
    CategoryToToolMap,
    IgniSettingsPermissionsFile,
    KeyValueGlobPattern,
    LoadResult,
    PermissionRule,
    RuleArgPattern,
    ToolInvocation,
    ToolInvocationArgs,
    ToolPermissionDefaults,
    ToolPermissions,
)
from igni_core.core.config.tool_permissions import schemas as facade_schemas


def _args(**raw):
    return ToolInvocationArgs.from_dict(raw)


# ── the arguments a rule reads ────────────────────────────────────


def test_a_call_with_no_arguments_is_empty_and_dumps_only_what_was_given():
    for raw in (None, {}):
        args = ToolInvocationArgs.from_dict(raw)

        assert args.is_empty()
        assert args.as_dict() == {}
        assert args.primary_string() == ""
        assert args.domain() == ""
        assert args.path_string() == ""

    full = _args(command="ls", mystery=3)
    assert not full.is_empty()
    assert full.as_dict() == {"command": "ls", "mystery": 3}


def test_the_primary_string_prefers_the_argument_list_then_the_first_path_like_field():
    assert _args(args=["git", "log"], path="p", url="u").primary_string() == "git log"
    # An empty list is no list: the path-like fields are next, in order.
    assert _args(args=[], file_name="n", url="u", query="q").primary_string() == "n"
    assert _args(file_path="f", file_name="n").primary_string() == "f"
    assert _args(url="https://a.test/", query="q").primary_string() == "https://a.test/"
    assert _args(query="pydantic").primary_string() == "pydantic"
    # No known field at all: every value, extras included, joined.
    assert _args(mystery="x", other="y").primary_string() == "x y"


def test_an_argument_that_is_not_a_string_still_reaches_the_matchers():
    """A model can send ``"path": 5`` or ``"args": ["ls", 1]``. The check must
    read them as text, not raise a validation error in the middle of a run."""
    assert _args(args=["ls", 1]).primary_string() == "ls 1"
    assert _args(path=5).path_string() == "5"
    assert _args(args="ls -l").primary_string() == "ls -l"

    perms = ToolPermissions()
    assert perms.check("Bash", tool_args={"command": 7}) == "ask"
    assert perms.check("Edit", tool_args={"path": 5, "args": "x"}) == "ask"


def test_the_domain_comes_from_the_url_then_the_query_and_is_empty_when_there_is_none():
    assert _args(url="https://user@host.test:8080/a").domain() == "user@host.test:8080"
    assert _args(query="https://q.test/x").domain() == "q.test"
    assert _args(url="https://u.test", query="https://q.test").domain() == "u.test"
    assert _args(url="not a url").domain() == ""
    # A malformed IPv6 host makes urlparse raise; that is "no domain", not a crash.
    assert _args(url="http://[::1").domain() == ""


def test_the_path_string_is_the_first_of_path_file_path_and_file_name():
    assert _args(path="a", file_path="b", file_name="c").path_string() == "a"
    assert _args(file_path="b", file_name="c").path_string() == "b"
    assert _args(file_name="c").path_string() == "c"
    assert _args(url="https://a.test").path_string() == ""


# ── the kinds of rule ─────────────────────────────────────────────


def test_the_pattern_base_refuses_to_decide():
    with pytest.raises(NotImplementedError):
        RuleArgPattern(raw="x").matches(_args())


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("Bash", "BarePattern"),
        ("Bash(git status)", "GlobPattern"),
        ("Bash(git *)", "GlobPattern"),
        ("WebFetch(domain:github.com)", "DomainPattern"),
        ("Edit(path:src/*)", "PathPattern"),
        ("Bash(git:push)", "KeyValueGlobPattern"),
    ],
)
def test_a_rule_string_becomes_the_kind_of_pattern_it_names(raw, kind):
    rule = PermissionRule.parse(raw, level="deny")

    assert rule is not None
    assert type(rule.arg_pattern).__name__ == kind
    assert rule.level == "deny"


@pytest.mark.parametrize("raw", ["", "   ", "Tool()", "Bash(a(b))x", "Two Words", "(x)", "Bash("])
def test_a_string_that_is_not_a_rule_is_not_parsed(raw):
    assert PermissionRule.parse(raw) is None


def test_a_rule_defaults_to_ask_and_a_second_colon_stays_in_the_value():
    rule = PermissionRule.parse("  Bash(git:push:*)  ")

    assert rule is not None
    assert rule.level == "ask"
    assert isinstance(rule.arg_pattern, KeyValueGlobPattern)
    assert (rule.arg_pattern.key, rule.arg_pattern.value) == ("git", "push:*")
    domain = PermissionRule.parse("WebFetch(domain:a:b)")
    assert domain is not None
    assert domain.arg_pattern.value == "a:b"  # type: ignore[attr-defined]


def test_a_rule_applies_only_to_its_own_tool():
    rule = PermissionRule.parse("Bash(git *)")
    assert rule is not None

    assert rule.matches("Bash", _args(command="git status"))
    assert not rule.matches("Edit", _args(command="git status"))
    assert not rule.matches("Bash", _args(command="rm x"))


def test_a_key_value_rule_matches_the_key_and_value_as_words_or_the_text_as_written():
    legacy = PermissionRule.parse("Bash(git:status)")
    assert legacy is not None
    assert legacy.matches("Bash", _args(command="git status"))
    assert not legacy.matches("Bash", _args(command="git push"))

    # The key holds a space: the whole text is the pattern.
    spaced = PermissionRule.parse("Bash(git push origin:main)")
    assert spaced is not None
    assert spaced.matches("Bash", _args(command="git push origin:main"))
    assert not spaced.matches("Bash", _args(command="git push origin main"))


def test_an_exact_rule_for_a_url_or_a_windows_path_matches_the_call_it_was_made_from():
    """``WebFetch(https://host/a)`` parses as ``key:value`` (``https`` and
    ``//host/a``) yet is the text of the call. "Allow" on that call must cover
    it, or the person is asked again every time."""
    for tool, raw in (
        ("WebFetch", {"url": "https://example.org/a/b?c=1"}),
        ("Edit", {"path": "C:\\work\\main.py"}),
        ("Bash", {"command": "echo a:b"}),
    ):
        invocation = ToolInvocation.from_raw(tool, raw)
        rule = PermissionRule.parse(invocation.exact_rule(), level="allow")
        assert rule is not None
        assert rule.matches(tool, invocation.args), invocation.exact_rule()

    rule = PermissionRule.parse("WebFetch(https://example.org/a)")
    assert rule is not None
    assert not rule.matches("WebFetch", _args(url="https://example.org/b"))


def test_glob_characters_in_a_rule_are_a_pattern_not_literal_text():
    rule = PermissionRule.parse("Bash(ls [ab]?)")
    assert rule is not None

    assert rule.matches("Bash", _args(command="ls a1"))
    assert not rule.matches("Bash", _args(command="ls c1"))


def test_a_domain_rule_is_a_glob_on_the_host_and_a_path_rule_on_the_first_path_field():
    domain = PermissionRule.parse("WebFetch(domain:*.github.com)")
    path = PermissionRule.parse("Edit(path:src/*)")
    assert domain is not None and path is not None

    assert domain.matches("WebFetch", _args(url="https://api.github.com/x"))
    assert not domain.matches("WebFetch", _args(url="https://github.com/x"))
    assert not domain.matches("WebFetch", _args())
    assert path.matches("Edit", _args(file_name="src/a.py"))
    assert not path.matches("Edit", _args(path="docs/a.md"))
    # No normalisation: a traversal still starts with the allowed prefix.
    assert path.matches("Edit", _args(path="src/../../etc/passwd"))


# ── defaults and categories ───────────────────────────────────────


def test_the_default_levels_ask_for_every_tool_that_changes_something_or_reaches_out():
    defaults = ToolPermissionDefaults()

    assert defaults.levels == {
        "Write": "ask",
        "Edit": "ask",
        "Bash": "ask",
        "BashOutput": "ask",
        "WebSearch": "ask",
        "WebFetch": "ask",
        "NotebookEdit": "ask",
    }


def test_an_unknown_tool_gets_the_fallback_and_a_snapshot_is_independent():
    defaults = ToolPermissionDefaults()

    assert defaults.for_tool("Nope") == "ask"
    assert defaults.for_tool("Nope", "deny") == "deny"
    assert defaults.for_tool("Bash", "deny") == "ask"

    snapshot = defaults.as_dict()
    snapshot["Bash"] = "allow"
    assert defaults.for_tool("Bash") == "ask"


def test_the_category_map_names_the_tools_each_category_governs():
    cmap = CategoryToToolMap()

    assert cmap.canonical == {
        PermissionCategory.FILE_READ: ["Read", "Glob", "Grep", "LS"],
        PermissionCategory.FILE_WRITE: ["Write", "Edit"],
        PermissionCategory.SHELL_EXECUTE: ["Bash", "BashOutput", "Python"],
    }
    assert cmap.web_search_tools == ["WebSearch"]
    assert cmap.web_fetch_tools == ["WebFetch"]


def test_the_config_levels_are_those_the_config_sets_and_the_fields_are_all_five():
    cmap = CategoryToToolMap()
    cfg = PermissionsConfig(file_read="deny", file_write="allow", shell_execute="ask")

    levels = cmap.iter_config_levels(cfg)

    assert levels == [
        ("deny", ["Read", "Glob", "Grep", "LS"]),
        ("allow", ["Write", "Edit"]),
        ("ask", ["Bash", "BashOutput", "Python"]),
        ("ask", ["WebSearch"]),
        ("ask", ["WebFetch"]),
    ]
    # A category whose level the config leaves unset is skipped.
    unset = PermissionsConfig.model_construct(
        file_read=None, file_write="allow", shell_execute=None, web_search=None, web_fetch="deny"
    )
    assert cmap.iter_config_levels(unset) == [("allow", ["Write", "Edit"]), ("deny", ["WebFetch"])]
    # The field names are what ``final_fields`` records.
    assert [name for name, _ in cmap.iter_config_fields(cfg)] == [
        "file_read",
        "file_write",
        "shell_execute",
        "web_search",
        "web_fetch",
    ]
    assert cmap.iter_config_fields(cfg)[3] == ("web_search", ["WebSearch"])


# ── the settings file's wire shape ────────────────────────────────


def test_a_settings_file_lists_default_empty_and_flatten_in_allow_ask_deny_order():
    assert IgniSettingsPermissionsFile().rules_by_level() == []

    file = IgniSettingsPermissionsFile(allow=["A", "B"], ask=["C"], deny=["D"])

    assert file.rules_by_level() == [("allow", "A"), ("allow", "B"), ("ask", "C"), ("deny", "D")]


def test_a_settings_file_refuses_rules_that_are_not_text():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        IgniSettingsPermissionsFile(allow="Bash")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        IgniSettingsPermissionsFile(deny=[1])  # type: ignore[list-item]


def test_a_load_result_says_where_whether_and_why():
    ok = LoadResult(path="/p", ok=True, file=IgniSettingsPermissionsFile())
    bad = LoadResult(path="/q", ok=False, reason="nope")

    assert (ok.path, ok.ok, ok.reason) == ("/p", True, None)
    assert (bad.file, bad.reason) == (None, "nope")


# ── the package facade ────────────────────────────────────────────


def test_the_func_to_tool_view_is_read_only_and_lists_the_resolvers_table():
    from igni_core.core.config.tool_permissions import ToolNameResolver

    with pytest.raises(TypeError):
        FUNC_TO_TOOL["run_shell_command"] = "Other"  # type: ignore[index]
    assert dict(FUNC_TO_TOOL) == dict(ToolNameResolver._MAP)


def test_every_name_the_facade_exports_exists_and_the_schema_types_are_the_real_ones():
    import igni_core.core.config.tool_permissions as facade

    assert len(facade.__all__) == len(set(facade.__all__))
    for name in facade.__all__:
        assert hasattr(facade, name), name
    assert facade.PermissionRule is facade_schemas.PermissionRule
    assert facade.ToolInvocation is ToolInvocation
    assert set(facade.__all__) >= {
        "ToolPermissions",
        "SettingsFileLoader",
        "SettingsFileWriter",
        "FUNC_TO_TOOL",
    }
