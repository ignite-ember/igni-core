"""How a tool result is clipped before the model reads it.

The shell and the process supervisor hand every result through
``LLMResultBuffer``; the model sees the head, a marker and the tail.
"""

from __future__ import annotations

import pytest

from igni_core.core.tools.tool_result import LLMResultBuffer


class TestWhatFitsPassesThrough:
    def test_text_under_the_cap_is_returned_as_it_is(self):
        assert LLMResultBuffer(max_chars=10).truncate("short") == "short"

    def test_text_exactly_at_the_cap_is_returned_as_it_is(self):
        assert LLMResultBuffer(max_chars=5).truncate("12345") == "12345"

    def test_empty_text_stays_empty(self):
        assert LLMResultBuffer(max_chars=4).truncate("") == ""


class TestWhatOverflowsIsCutInTheMiddle:
    def test_one_character_over_keeps_both_halves_and_counts_the_cut(self):
        text = "abcdefghijk"  # 11 characters, cap 10

        assert LLMResultBuffer(max_chars=10).truncate(text) == (
            "abcde\n\n... (1 characters truncated) ...\n\nghijk"
        )

    def test_the_marker_counts_what_was_over_the_cap(self):
        out = LLMResultBuffer(max_chars=10).truncate("a" * 5 + "m" * 90 + "z" * 5)

        assert out == "aaaaa\n\n... (90 characters truncated) ...\n\nzzzzz"

    def test_characters_are_counted_as_code_points(self):
        text = "\U0001f600" * 6

        out = LLMResultBuffer(max_chars=4).truncate(text)

        assert (
            out
            == "\U0001f600\U0001f600\n\n... (2 characters truncated) ...\n\n\U0001f600\U0001f600"
        )


class TestTheCap:
    def test_the_default_cap_is_thirty_thousand(self):
        buf = LLMResultBuffer()

        assert LLMResultBuffer.MAX_CHARS == 30_000
        assert buf.max_chars == 30_000
        assert buf.truncate("x" * 30_000) == "x" * 30_000
        assert "characters truncated" in buf.truncate("x" * 30_001)

    def test_a_constructor_value_overrides_the_default(self):
        assert LLMResultBuffer(max_chars=7).max_chars == 7

    def test_a_per_call_limit_overrides_the_instance_cap(self):
        buf = LLMResultBuffer(max_chars=100)

        assert buf.truncate("abcdefghijk", limit=10).startswith("abcde\n\n... (1 ")
        assert buf.truncate("abcdefghijk") == "abcdefghijk"
        assert buf.max_chars == 100

    @pytest.mark.parametrize("cap", [0, 1])
    def test_a_cap_too_small_for_two_halves_keeps_no_text_and_says_so(self, cap):
        out = LLMResultBuffer().truncate("abcdef", limit=cap)

        assert out == f"\n\n... ({6 - cap} characters truncated) ...\n\n"
        assert "abcdef" not in out

    def test_a_limit_of_zero_is_a_limit_not_the_instance_cap(self):
        out = LLMResultBuffer(max_chars=100).truncate("abc", limit=0)

        assert out == "\n\n... (3 characters truncated) ...\n\n"
