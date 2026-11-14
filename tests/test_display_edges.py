"""The terminal display's edges: text that looks like Rich markup, the
minute boundary of a run's summary, and where a long argument is clipped."""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from igni_core.core.utils.display import DisplayManager, RunStats, ToolCallDisplay


def _make(width: int = 120) -> tuple[DisplayManager, StringIO]:
    buf = StringIO()
    return DisplayManager(console=Console(file=buf, no_color=True, width=width)), buf


class TestTextThatLooksLikeMarkup:
    """A provider's error text is not the display's markup."""

    def test_an_error_with_a_closing_tag_is_printed_as_it_is(self):
        mgr, buf = _make()
        mgr.print_error("the model replied [/INST] and stopped")
        assert buf.getvalue() == "Error: the model replied [/INST] and stopped\n"

    def test_an_error_with_a_style_tag_is_printed_as_it_is(self):
        mgr, buf = _make()
        mgr.print_error("got [red]x[/red]")
        assert buf.getvalue() == "Error: got [red]x[/red]\n"

    def test_a_warning_is_printed_as_it_is(self):
        mgr, buf = _make()
        mgr.print_warning("see [/docs]")
        assert buf.getvalue() == "Warning: see [/docs]\n"

    def test_an_info_line_is_printed_as_it_is(self):
        mgr, buf = _make()
        mgr.print_info("path [/tmp/x]")
        assert buf.getvalue() == "path [/tmp/x]\n"

    def test_an_agent_name_is_printed_as_it_is(self):
        mgr, buf = _make()
        mgr.print_response("hi", agent_name="[/x] helper")
        assert buf.getvalue().splitlines()[0] == "[/x] helper"

    def test_a_tool_call_argument_is_printed_as_it_is(self):
        mgr, buf = _make()
        mgr.print_tool_call(ToolCallDisplay(tool_name="grep", args={"pattern": "[/a-z]+"}))
        assert buf.getvalue() == "  > grep (pattern=[/a-z]+)\n"

    def test_a_model_name_in_the_run_summary_is_printed_as_it_is(self):
        mgr, buf = _make()
        mgr.print_run_stats(RunStats(elapsed_seconds=1.0, model="m[/x]"))
        assert buf.getvalue() == "  ── 1.0s · m[/x] ──\n"

    def test_a_version_in_the_welcome_banner_is_printed_as_it_is(self):
        mgr, buf = _make()
        mgr.print_welcome("1.0[/x]")
        assert "igni v1.0[/x]" in buf.getvalue()


class TestTheBannerAndTheDefaultConsole:
    def test_the_welcome_banner_says_what_to_type_and_how_to_leave(self):
        mgr, buf = _make()
        mgr.print_welcome("2.3.4")
        out = buf.getvalue()
        assert "igni v2.3.4" in out
        assert "AI coding assistant powered by Agno" in out
        assert "Type your message, or /help for commands." in out
        assert "Press Ctrl+C to exit." in out

    def test_without_a_console_the_manager_makes_its_own(self):
        assert isinstance(DisplayManager().console, Console)

    def test_the_manager_re_exports_its_two_models(self):
        from igni_core.core.utils import display

        assert display.__all__ == ["DisplayManager", "RunStats", "ToolCallDisplay"]

    def test_no_agent_name_prints_no_label(self):
        mgr, buf = _make()
        mgr.print_response("body", agent_name="")
        assert buf.getvalue().strip() == "body"


class TestTheRunSummary:
    @pytest.mark.parametrize(
        ("seconds", "text"),
        [
            (0, "0.0s"),
            (59.94, "59.9s"),
            (59.96, "1m 0s"),
            (60, "1m 0s"),
            (61.9, "1m 1s"),
            (3725, "62m 5s"),
        ],
    )
    def test_the_minute_boundary(self, seconds, text):
        assert RunStats(elapsed_seconds=seconds).format_summary() == text

    def test_only_one_token_count_is_enough_to_show_the_tokens(self):
        assert (
            RunStats(elapsed_seconds=1, output_tokens=7).format_summary()
            == "1.0s · 7 tokens (0↑ 7↓)"
        )
        assert (
            RunStats(elapsed_seconds=1, input_tokens=9).format_summary()
            == "1.0s · 9 tokens (9↑ 0↓)"
        )

    def test_the_segments_are_in_order(self):
        stats = RunStats(elapsed_seconds=2, input_tokens=1, output_tokens=2, model="m")
        assert stats.format_summary() == "2.0s · 3 tokens (1↑ 2↓) · m"


class TestTheToolCallArguments:
    def test_several_arguments_are_joined_in_order(self):
        call = ToolCallDisplay(tool_name="t", args={"a": 1, "b": None, "c": [1, 2]})
        assert call.format_args() == " (a=1, b=None, c=[1, 2])"

    def test_a_value_of_exactly_the_limit_is_kept(self):
        call = ToolCallDisplay(tool_name="t", args={"k": "x" * 50})
        assert call.format_args() == f" (k={'x' * 50})"

    def test_a_value_one_over_the_limit_is_clipped_to_the_limit(self):
        call = ToolCallDisplay(tool_name="t", args={"k": "x" * 51})
        assert call.format_args() == f" (k={'x' * 47}...)"

    def test_the_limit_can_be_chosen(self):
        call = ToolCallDisplay(tool_name="t", args={"k": "abcdefghij"})
        assert call.format_args(max_value_len=8) == " (k=abcde...)"

    @pytest.mark.parametrize("limit", [0, 1, 2, 3])
    def test_a_limit_too_small_for_the_ellipsis_still_ends_in_it_alone(self, limit):
        call = ToolCallDisplay(tool_name="t", args={"k": "abcdefghij"})
        assert call.format_args(max_value_len=limit) == " (k=...)"

    def test_a_unicode_value_is_clipped_by_characters(self):
        call = ToolCallDisplay(tool_name="t", args={"k": "é" * 60})
        assert call.format_args() == f" (k={'é' * 47}...)"
