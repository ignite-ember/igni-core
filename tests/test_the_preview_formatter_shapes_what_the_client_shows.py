"""The preview formatter's limits: the client-facing truncation protocol."""

from __future__ import annotations

from igni_core.core.tools.orchestrate_preview import PREVIEWS, PreviewFormatter


class TestFormatArgs:
    def test_nothing_gives_an_empty_string(self):
        assert PREVIEWS.format_args(None) == ""
        assert PREVIEWS.format_args({}) == ""

    def test_only_the_first_two_kwargs_are_shown(self):
        assert PREVIEWS.format_args({"a": 1, "b": 2, "c": 3}) == "a=1, b=2"
        assert PREVIEWS.format_args({"a": 1}) == "a=1"

    def test_a_value_of_thirty_chars_is_whole_and_thirty_one_is_cut(self):
        assert PREVIEWS.format_args({"k": "x" * 30}) == "k=" + "x" * 30
        assert PREVIEWS.format_args({"k": "x" * 31}) == "k=" + "x" * 27 + "..."

    def test_newlines_become_spaces_and_values_are_stringified(self):
        assert PREVIEWS.format_args({"k": "a\nb", "n": 5}) == "k=a b, n=5"


class TestFormatFullArgs:
    def test_every_kwarg_is_shown_untruncated(self):
        long = "y" * 100
        assert PREVIEWS.format_full_args({"a": 1, "b": 2, "c": long}) == f"a=1, b=2, c={long}"

    def test_nothing_gives_an_empty_string(self):
        assert PREVIEWS.format_full_args(None) == ""
        assert PREVIEWS.format_full_args({}) == ""


class TestFormatResult:
    def test_none_is_empty_but_an_empty_string_stays_empty_too(self):
        assert PREVIEWS.format_result(None) == ""
        assert PREVIEWS.format_result("") == ""

    def test_the_default_cap_is_sixty(self):
        assert PREVIEWS.format_result("r" * 60) == "r" * 60
        assert PREVIEWS.format_result("r" * 61) == "r" * 60 + "..."

    def test_a_given_limit_replaces_the_default(self):
        assert PREVIEWS.format_result("abcdef", limit=3) == "abc..."
        assert PREVIEWS.format_result("abc", limit=3) == "abc"

    def test_a_limit_of_zero_cuts_everything(self):
        assert PREVIEWS.format_result("abc", limit=0) == "..."

    def test_it_is_one_line_and_stripped(self):
        assert PREVIEWS.format_result("  a\nb  ") == "a b"

    def test_a_non_string_result_is_stringified(self):
        assert PREVIEWS.format_result({"k": 1}) == "{'k': 1}"


class TestFormatContentBuffer:
    def test_nothing_visible_gives_an_empty_string(self):
        assert PREVIEWS.format_content_buffer("") == ""
        assert PREVIEWS.format_content_buffer(" \n\n  \n") == ""
        assert PREVIEWS.format_content_buffer("<think></think>") == ""

    def test_the_last_five_non_empty_lines_are_kept(self):
        buf = "\n".join(f"line{i}\n" for i in range(8))
        assert PREVIEWS.format_content_buffer(buf) == "\n".join(f"line{i}" for i in range(3, 8))

    def test_lines_are_stripped_and_crlf_is_handled(self):
        assert PREVIEWS.format_content_buffer("  a  \r\n\r\n b\r\n") == "a\nb"

    def test_a_line_of_120_is_whole_and_121_is_cut_with_an_ellipsis(self):
        assert PREVIEWS.format_content_buffer("z" * 120) == "z" * 120
        assert PREVIEWS.format_content_buffer("z" * 121) == "z" * 119 + "…"

    def test_think_tags_are_removed_as_literals(self):
        assert PREVIEWS.format_content_buffer("<think>hmm</think>\nanswer") == "hmm\nanswer"
        assert PREVIEWS.format_content_buffer("<think>never closed") == "never closed"

    def test_unicode_lines_are_kept(self):
        assert PREVIEWS.format_content_buffer("héllo ✓") == "héllo ✓"


class TestTheFormatter:
    def test_the_shared_instance_carries_the_documented_limits(self):
        assert isinstance(PREVIEWS, PreviewFormatter)
        assert (
            PREVIEWS.WINDOW,
            PREVIEWS.LINE_MAX,
            PREVIEWS.ARGS_KWARG_COUNT,
            PREVIEWS.ARGS_VALUE_MAX,
            PREVIEWS.RESULT_DEFAULT_LIMIT,
        ) == (5, 120, 2, 30, 60)

    def test_a_subclass_can_widen_the_window_without_touching_the_shared_one(self):
        class Wide(PreviewFormatter):
            WINDOW = 2

        buf = "a\nb\nc"
        assert Wide().format_content_buffer(buf) == "b\nc"
        assert PREVIEWS.format_content_buffer(buf) == "a\nb\nc"
