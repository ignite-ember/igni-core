"""The models that read a plugin's third-party files: frontmatter, hooks and MCP servers.

They are fed the text a plugin author wrote, so each pins what is read from
awkward input: keys that repeat, quotes, an unclosed block, Windows line endings,
a hooks value that is not a list, a server with only a URL.
"""

from __future__ import annotations

from igni_core.backend.plugin_bundle_files import (
    MarkdownFrontmatter,
    PluginHooksFile,
    PluginMCPFile,
    PluginMCPServerEntry,
)


def test_frontmatter_keys_are_lower_cased_and_values_stripped_of_space_and_quotes():
    fm = MarkdownFrontmatter.parse('---\nName:   "Deploy"  \nDescription: plain\n---\nbody')

    assert fm.values == {"name": "Deploy", "description": "plain"}


def test_the_first_occurrence_of_a_key_wins():
    fm = MarkdownFrontmatter.parse("---\ndescription: first\ndescription: second\n---\n")

    assert fm.value("description") == "first"


def test_a_value_keeps_the_colons_after_the_first():
    fm = MarkdownFrontmatter.parse("---\ndescription: Use when: it breaks (see http://x)\n---\n")

    assert fm.value("description") == "Use when: it breaks (see http://x)"


def test_lookup_ignores_case_and_a_missing_key_is_empty():
    fm = MarkdownFrontmatter.parse("---\ndescription: d\n---\n")

    assert fm.value("DESCRIPTION") == "d"
    assert fm.value("name") == ""


def test_text_without_a_leading_block_or_with_an_unclosed_one_has_no_values():
    for text in ("", "no block", "\n---\ndescription: d\n---\n", "---\ndescription: d\n"):
        assert MarkdownFrontmatter.parse(text).values == {}


def test_lines_without_a_colon_are_skipped():
    fm = MarkdownFrontmatter.parse("---\njust words\nname: x\n---\n")

    assert fm.values == {"name": "x"}


def test_windows_line_endings_leave_no_carriage_return_in_a_value():
    fm = MarkdownFrontmatter.parse("---\r\nname: x\r\ndescription: y\r\n---\r\nbody")

    assert fm.values == {"name": "x", "description": "y"}


def test_hooks_list_one_entry_per_event_with_a_list_of_handlers():
    file = PluginHooksFile.model_validate_json(
        '{"hooks": {"PreToolUse": [{}, {}], "Stop": [], "Odd": "x", "Dict": {"a": 1}}}'
    )

    assert [(h.event, h.count) for h in file.hook_infos()] == [("PreToolUse", 2), ("Stop", 0)]


def test_a_hooks_file_with_null_or_missing_hooks_lists_nothing():
    assert PluginHooksFile.model_validate_json('{"hooks": null}').hook_infos() == []
    assert PluginHooksFile.model_validate_json("{}").hook_infos() == []


def test_a_server_names_its_transport_or_is_stdio_and_launches_by_command_then_url():
    cases = [
        (PluginMCPServerEntry(), ("stdio", "")),
        (PluginMCPServerEntry(command="node x"), ("stdio", "node x")),
        (PluginMCPServerEntry(type="http", url="https://h/mcp"), ("http", "https://h/mcp")),
        (PluginMCPServerEntry(command="run", url="https://h"), ("stdio", "run")),
    ]
    for entry, expected in cases:
        info = entry.to_info("srv")
        assert (info.name, info.transport, info.command) == ("srv", *expected)


def test_servers_are_listed_in_file_order_under_either_spelling_of_the_key():
    text = '{"mcpServers": {"b": {"command": "1"}, "a": {"command": "2"}}}'

    by_alias = PluginMCPFile.model_validate_json(text)
    by_name = PluginMCPFile.model_validate({"mcp_servers": {"z": {"command": "3"}}})

    assert [s.name for s in by_alias.server_infos()] == ["b", "a"]
    assert [s.name for s in by_name.server_infos()] == ["z"]


def test_a_null_or_missing_server_map_lists_nothing():
    assert PluginMCPFile.model_validate_json('{"mcpServers": null}').server_infos() == []
    assert PluginMCPFile.model_validate_json("{}").server_infos() == []


def test_a_dash_run_inside_a_value_does_not_close_the_block():
    fm = MarkdownFrontmatter.parse("---\ndescription: before --- after\nname: x\n---\n")

    assert fm.values == {"description": "before --- after", "name": "x"}
