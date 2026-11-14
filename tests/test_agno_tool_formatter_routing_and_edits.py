"""Tests for the registry, friendly names and the ``edit_file`` branch of
:class:`AgnoToolEventFormatter`.

``test_agno_events.py`` and ``test_extract_result_summary.py`` pin the
argument summaries and the generic result summary. What is pinned here:

* which formatter each tool name routes to, that a registration replaces the
  previous one, and that every registry owns its own table;
* the display name of a tool and its fallbacks;
* what a successful ``edit_file`` shows (a diff, with a summary and timing),
  and that a failed or row-less edit keeps the plain summary instead.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from igni_core.protocol.agno_tool_formatter import (
    AgnoToolEventFormatter,
    DefaultFormatter,
    SpawnFormatter,
    ToolCallFormatter,
    ToolCallFormatterRegistry,
)
from igni_core.protocol.edit_diff_computer import (
    INSERT_STYLE,
    EditDiffComputer,
)
from igni_core.protocol.tool_friendly_names import ToolFriendlyNames


class _Shout(ToolCallFormatter):
    def format_args(self, tool_args: dict[str, Any]) -> str:
        return "!" + ",".join(sorted(tool_args))


class _NoFile:
    def read(self, file_path: str) -> str:
        raise OSError(file_path)


def _edit_event(result: Any = "", duration: float | None = None, args: dict | None = None):
    metrics = SimpleNamespace(duration=duration) if duration is not None else None
    tool = SimpleNamespace(
        tool_name="edit_file",
        result=result,
        metrics=metrics,
        tool_args=args if args is not None else {"old_string": "a", "new_string": "b"},
    )
    return SimpleNamespace(tool=tool)


def _with_diffs() -> AgnoToolEventFormatter:
    return AgnoToolEventFormatter(diff_computer=EditDiffComputer(file_reader=_NoFile()))


class TestRegistryRouting:
    def test_spawn_tools_share_the_spawn_formatter(self):
        registry = ToolCallFormatterRegistry()
        assert isinstance(registry.formatter_for("spawn_agent"), SpawnFormatter)
        assert registry.formatter_for("spawn_team") is registry.formatter_for("spawn_agent")

    def test_every_other_tool_gets_the_default(self):
        registry = ToolCallFormatterRegistry()
        assert isinstance(registry.formatter_for("read_file"), DefaultFormatter)
        assert registry.formatter_for("read_file") is registry.formatter_for("anything_else")

    def test_a_registration_routes_that_tool_and_a_second_one_replaces_it(self):
        registry = ToolCallFormatterRegistry()
        registry.register("read_file", _Shout())
        assert registry.format_args("read_file", {"b": 1, "a": 2}) == "!a,b"
        replacement = DefaultFormatter()
        registry.register("read_file", replacement)
        assert registry.formatter_for("read_file") is replacement
        assert registry.format_args("read_file", {"a": 2}) == "a=2"

    def test_a_registration_can_replace_a_spawn_route(self):
        registry = ToolCallFormatterRegistry()
        registry.register("spawn_agent", _Shout())
        assert registry.format_args("spawn_agent", {"task": "t"}) == "!task"
        # spawn_team keeps its own route.
        assert registry.format_args("spawn_team", {"agent_name": "a"}) == "a"

    def test_empty_or_non_dict_args_are_an_empty_summary_even_when_routed(self):
        registry = ToolCallFormatterRegistry()
        registry.register("read_file", _Shout())
        for args in (None, {}, [], "x"):
            assert registry.format_args("read_file", args) == ""

    def test_each_registry_owns_its_table(self):
        first = ToolCallFormatterRegistry()
        first.register("read_file", _Shout())
        second = ToolCallFormatterRegistry()
        assert isinstance(second.formatter_for("read_file"), DefaultFormatter)
        assert second.formatter_for("spawn_agent") is not first.formatter_for("spawn_agent")

    def test_a_formatter_built_with_no_registry_is_not_changed_by_anothers(self):
        custom = ToolCallFormatterRegistry()
        custom.register("read_file", _Shout())
        routed = AgnoToolEventFormatter(registry=custom)
        plain = AgnoToolEventFormatter()
        assert routed.args_summary("read_file", {"p": 1}) == "!p"
        assert plain.args_summary("read_file", {"p": 1}) == "p=1"
        assert AgnoToolEventFormatter().args_summary("read_file", {"p": 1}) == "p=1"


class TestFriendlyName:
    def test_a_known_tool_gets_the_catalog_name(self):
        formatter = AgnoToolEventFormatter()
        names = ToolFriendlyNames.all_names()
        assert names
        for raw, shown in names.items():
            assert formatter.friendly_name(raw) == shown
            assert formatter.friendly_name(raw, default="zzz") == shown

    def test_the_names_a_user_sees_are_unchanged(self):
        formatter = AgnoToolEventFormatter()
        assert formatter.friendly_name("read_file") == "Read"
        assert formatter.friendly_name("save_file") == "Write"
        assert formatter.friendly_name("edit_file") == "Edit"
        assert formatter.friendly_name("edit_file_replace_all") == "Edit"

    def test_an_unknown_tool_keeps_its_name_or_takes_the_default(self):
        formatter = AgnoToolEventFormatter()
        assert formatter.friendly_name("my_custom_tool") == "my_custom_tool"
        assert formatter.friendly_name("my_custom_tool", default="Custom") == "Custom"


class TestEditFileResult:
    def test_a_successful_edit_is_a_diff_with_the_result_as_summary(self):
        result = _with_diffs().extract_result(_edit_event(result="Edited a.py"))
        assert result.has_markup is True
        assert result.full_result == ""
        assert result.summary == "Edited a.py"
        assert result.diff_rows == [
            ("-    1   a", "#ff6b6b on #3d0000"),
            ("+    1   b", INSERT_STYLE),
        ]

    def test_the_summary_defaults_to_edited_and_gets_the_timing(self):
        result = _with_diffs().extract_result(_edit_event(result="", duration=0.5))
        assert result.summary == "Edited, 0.50s"
        assert result.has_markup is True

    def test_a_result_text_gets_the_timing_appended(self):
        result = _with_diffs().extract_result(_edit_event(result="Done", duration=1.0))
        assert result.summary == "Done, 1.00s"

    def test_a_zero_duration_still_shows_a_timing(self):
        result = _with_diffs().extract_result(_edit_event(result="Done", duration=0.0))
        assert result.summary == "Done, 0.00s"

    def test_a_failed_edit_keeps_its_error_prefix_and_shows_no_diff(self):
        result = _with_diffs().extract_result(_edit_event(result="Error: old_string not found"))
        assert result.has_markup is False
        assert result.diff_rows is None
        assert result.full_result == "Error: old_string not found"
        assert result.summary == "Error: old_string not found"

    def test_an_edit_with_nothing_to_diff_falls_back_to_the_summary(self):
        event = _edit_event(result="ok", args={"old_string": "", "new_string": ""})
        result = _with_diffs().extract_result(event)
        assert result.has_markup is False
        assert result.diff_rows is None
        assert result.summary == "ok"
        assert result.full_result == "ok"

    def test_an_edit_whose_args_are_malformed_falls_back_to_the_summary(self):
        event = _edit_event(result="ok", args={"old_string": 5, "new_string": None})
        result = _with_diffs().extract_result(event)
        assert result.has_markup is False
        assert result.summary == "ok"

    def test_without_a_diff_computer_an_edit_is_a_plain_result(self):
        result = AgnoToolEventFormatter().extract_result(_edit_event(result="Edited a.py"))
        assert result.has_markup is False
        assert result.diff_rows is None
        assert result.summary == "Edited a.py"

    def test_other_tools_never_get_a_diff(self):
        event = _edit_event(result="written")
        event.tool.tool_name = "write_file"
        result = _with_diffs().extract_result(event)
        assert result.has_markup is False
        assert result.summary == "written"


class TestResultValues:
    def test_a_result_that_is_not_text_is_shown_as_text(self):
        event = SimpleNamespace(
            tool=SimpleNamespace(tool_name="t", result=12345, metrics=None, tool_args={})
        )
        assert AgnoToolEventFormatter().extract_result(event).full_result == "12345"

    def test_a_falsy_result_is_empty(self):
        for falsy in (0, "", None, [], False):
            event = SimpleNamespace(
                tool=SimpleNamespace(tool_name="t", result=falsy, metrics=None, tool_args={})
            )
            assert AgnoToolEventFormatter().extract_result(event).full_result == ""

    def test_a_duration_of_zero_reads_as_a_timing(self):
        event = SimpleNamespace(
            tool=SimpleNamespace(
                tool_name="t", result="", metrics=SimpleNamespace(duration=0.0), tool_args={}
            )
        )
        assert AgnoToolEventFormatter().extract_result(event).summary == "completed in 0.00s"

    def test_an_event_with_no_tool_gives_an_empty_result(self):
        result = AgnoToolEventFormatter().extract_result(SimpleNamespace())
        assert (result.summary, result.full_result, result.has_markup) == ("", "", False)

    def test_a_trailing_newline_does_not_make_a_single_line_multi_line(self):
        event = SimpleNamespace(
            tool=SimpleNamespace(tool_name="t", result="one line\n", metrics=None, tool_args={})
        )
        assert AgnoToolEventFormatter().extract_result(event).summary == "one line"
