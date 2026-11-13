"""The display name each raw tool name is shown as, and what an unknown one falls back to."""

from __future__ import annotations

import pytest

from igni_core.protocol.tool_friendly_names import ToolFriendlyNames


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("read_file", "Read"),
        ("save_file", "Write"),
        ("create_file", "Write"),
        ("edit_file", "Edit"),
        ("edit_file_replace_all", "Edit"),
        ("run_shell_command", "Bash"),
        ("grep_files", "Grep"),
        ("glob_files", "Glob"),
        ("list_files", "LS"),
        ("fetch_url", "WebFetch"),
        ("spawn_agent", "Agent"),
        ("cancel_scheduled_task", "Schedule"),
    ],
)
def test_a_raw_tool_name_is_shown_as_its_display_name(raw: str, shown: str) -> None:
    assert ToolFriendlyNames.friendly_name(raw) == shown


@pytest.mark.parametrize(
    "raw", ["web_search", "search_news", "duckduckgo_search", "duckduckgo_news"]
)
def test_the_current_and_the_old_web_search_names_are_all_web_search(raw: str) -> None:
    """A rule ``deny: ["WebSearch"]`` is resolved by inverting this map: the
    names the tool registers today must be in it, or the rule denies nothing."""
    assert ToolFriendlyNames.friendly_name(raw) == "WebSearch"


def test_an_unknown_tool_is_shown_by_its_raw_name() -> None:
    assert ToolFriendlyNames.friendly_name("my_custom_tool") == "my_custom_tool"


def test_an_unknown_tool_is_shown_as_the_default_given() -> None:
    assert ToolFriendlyNames.friendly_name("my_custom_tool", default="Custom") == "Custom"


def test_a_known_tool_ignores_the_default() -> None:
    assert ToolFriendlyNames.friendly_name("read_file", default="Custom") == "Read"


def test_an_empty_name_and_an_empty_default_are_kept_as_given() -> None:
    assert ToolFriendlyNames.friendly_name("") == ""
    assert ToolFriendlyNames.friendly_name("nope", default="") == ""


def test_the_catalog_is_read_only() -> None:
    names = ToolFriendlyNames.all_names()

    with pytest.raises(TypeError):
        names["read_file"] = "Cat"  # type: ignore[index]
    with pytest.raises(TypeError):
        del names["read_file"]  # type: ignore[attr-defined]
    assert ToolFriendlyNames.friendly_name("read_file") == "Read"


def test_the_catalog_holds_every_name_friendly_name_answers() -> None:
    names = ToolFriendlyNames.all_names()

    assert names["run_shell_command"] == "Bash"
    assert all(ToolFriendlyNames.friendly_name(raw) == shown for raw, shown in names.items())
