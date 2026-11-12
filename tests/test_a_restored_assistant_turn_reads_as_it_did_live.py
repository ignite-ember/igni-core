"""How a persisted assistant turn is rebuilt for a restored chat.

``split_content`` returns the thinking and the answer in the order the
person saw them; ``format_tool_args`` is the one-line ``key=value`` summary
on a restored tool card. ``test_plan_rehydrate`` pins the plain splits;
this pins the rest of both.
"""

from __future__ import annotations

import pytest

from igni_core.backend.restore_content import AssistantContentRestorer

split = AssistantContentRestorer.split_content
fmt = AssistantContentRestorer.format_tool_args


class TestSplitContent:
    def test_text_between_two_think_blocks_keeps_the_order(self):
        content = "intro <think>one</think> middle <think>two</think> outro"

        assert split(content) == [
            ("assistant", "intro"),
            ("thinking", "one"),
            ("assistant", "middle"),
            ("thinking", "two"),
            ("assistant", "outro"),
        ]

    def test_an_empty_think_block_adds_no_segment(self):
        assert split("before<think></think>after") == [
            ("assistant", "before"),
            ("assistant", "after"),
        ]

    def test_a_think_block_of_only_whitespace_adds_no_segment(self):
        assert split("<think>  \n </think>answer") == [("assistant", "answer")]

    def test_empty_content_has_no_segments(self):
        assert split("") == []

    def test_only_empty_think_blocks_have_no_segments(self):
        assert split("<think></think> <think></think>") == []

    def test_thinking_spans_lines_and_keeps_inner_markup(self):
        assert split("<think>a\n<b>x</b>\nc</think>done") == [
            ("thinking", "a\n<b>x</b>\nc"),
            ("assistant", "done"),
        ]

    def test_an_unclosed_block_after_an_answer_is_thinking_to_the_end(self):
        assert split("answer<think>cut off mid") == [
            ("assistant", "answer"),
            ("thinking", "cut off mid"),
        ]

    def test_unicode_survives(self):
        assert split("<think>日本語 ✓</think>héllo") == [
            ("thinking", "日本語 ✓"),
            ("assistant", "héllo"),
        ]


class TestFormatToolArgs:
    def test_strings_are_shown_raw_not_quoted(self):
        assert fmt({"command": "ls -la"}) == "command=ls -la"

    def test_pairs_are_joined_by_a_space_in_order(self):
        assert fmt({"b": 1, "a": 2}) == "b=1 a=2"

    def test_scalars_use_str(self):
        assert fmt({"n": 3, "f": 1.5, "t": True, "no": False, "none": None}) == (
            "n=3 f=1.5 t=True no=False none=None"
        )

    def test_a_container_is_compact_json(self):
        assert fmt({"o": {"a": [1, 2], "b": "x"}}) == 'o={"a":[1,2],"b":"x"}'

    def test_an_empty_dict_is_empty(self):
        assert fmt({}) == ""

    def test_a_string_of_exactly_the_cap_is_whole(self):
        value = "x" * 80

        assert fmt({"k": value}) == f"k={value}"

    def test_a_string_one_over_the_cap_is_elided_to_the_cap(self):
        out = fmt({"k": "x" * 81})

        assert out == "k=" + "x" * 77 + "..."
        assert len(out) - len("k=") == 80

    def test_a_container_over_the_cap_is_elided_too(self):
        out = fmt({"k": ["y" * 30] * 4})

        value = out.removeprefix("k=")
        assert len(value) == 80 and value.endswith("...") and value.startswith('["yyy')

    def test_one_long_value_does_not_drown_the_next(self):
        assert fmt({"a": "x" * 500, "b": "y"}).endswith("... b=y")

    def test_a_value_json_cannot_encode_falls_back_to_str(self):
        assert fmt({"k": {1, 2}}) == "k={1, 2}"

    def test_a_non_encodable_value_is_capped_as_well(self):
        out = fmt({"k": set(range(100))})

        assert len(out) - 2 == 80 and out.endswith("...")

    def test_a_list_is_compact_json_without_a_cap(self):
        items = list(range(60))

        assert fmt(items) == "[" + ",".join(map(str, items)) + "]"

    def test_a_list_json_cannot_encode_falls_back_to_str(self):
        assert fmt([{1}]) == "[{1}]"

    @pytest.mark.parametrize(
        ("args", "shown"),
        [(None, "None"), ("raw text", "raw text"), (7, "7"), (True, "True")],
    )
    def test_anything_else_is_str(self, args, shown):
        assert fmt(args) == shown
