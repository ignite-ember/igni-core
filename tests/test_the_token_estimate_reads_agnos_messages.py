"""How :class:`TokenEstimate` reads agno's messages and tool schemas.

``test_tokens_are_the_servers_count`` pins the headline: the server's count
up to the last answer, an estimate after it. These pin the readings that
feed the estimate: compressed content over content, structured content,
tool calls, the three shapes of a tool schema, an answer that was not
measured, and the rounding.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.core.session.token_estimate import TokenEstimate


def msg(role: str = "tool", content: Any = None, **fields: Any) -> SimpleNamespace:
    return SimpleNamespace(role=role, content=content, **fields)


def answer(read: int | None, wrote: int | None = 0, content: str = "") -> SimpleNamespace:
    return msg(
        "assistant",
        content,
        metrics=None
        if read is None and wrote is None
        else SimpleNamespace(input_tokens=read, output_tokens=wrote),
    )


class TestText:
    @pytest.mark.parametrize(
        ("length", "tokens"),
        [(-5, 0), (0, 0), (1, 0), (2, 1), (3, 1), (5, 1), (6, 2), (400, 100)],
    )
    def test_length_over_four_rounds_half_up_and_non_positive_is_zero(self, length, tokens):
        assert TokenEstimate.text(length) == tokens


class TestUncountedMessages:
    def test_no_messages_is_zero(self):
        assert TokenEstimate.of([]) == 0
        assert TokenEstimate.chars([]) == 0

    def test_a_message_without_content_costs_nothing(self):
        assert TokenEstimate.chars([msg(content=None), msg()]) == 0

    def test_compressed_content_is_what_is_counted_when_there_is_some(self):
        message = msg(content="x" * 4000, compressed_content="y" * 40)

        assert TokenEstimate.chars([message]) == 10

    def test_empty_compressed_content_falls_back_to_the_content(self):
        message = msg(content="x" * 40, compressed_content="")

        assert TokenEstimate.chars([message]) == 10

    def test_structured_content_is_counted_as_its_json(self):
        content = [{"type": "text", "text": "hello"}]

        assert TokenEstimate.chars([msg(content=content)]) == TokenEstimate.text(
            len(json.dumps(content))
        )

    def test_content_json_cannot_serialise_is_counted_as_its_string(self):
        class Opaque:
            def __str__(self) -> str:
                return "o" * 38

        # json.dumps(default=str) wraps the string in two quotes.
        assert TokenEstimate.chars([msg(content={"k": Opaque()})]) == TokenEstimate.text(
            len(json.dumps({"k": "o" * 38}))
        )

    def test_tool_calls_add_their_json_length_to_the_content(self):
        call = {"id": "c1", "function": {"name": "read", "arguments": "{}"}}
        message = msg("assistant", "x" * 40, tool_calls=[call])

        assert TokenEstimate.chars([message]) == TokenEstimate.text(40 + len(json.dumps(call)))

    def test_lengths_are_summed_before_rounding(self):
        # 2 chars each: 4 chars is one token, not two rounds of half-up zero.
        assert TokenEstimate.chars([msg(content="ab"), msg(content="cd")]) == 1

    def test_unicode_is_counted_in_characters(self):
        assert TokenEstimate.chars([msg(content="ü" * 40)]) == 10

    def test_nothing_measured_estimates_every_message_and_the_tools(self):
        conversation = [msg("user", "u" * 400), answer(None, None, "a" * 400)]

        assert TokenEstimate.of(conversation) == 200
        assert TokenEstimate.of(conversation, [{"n": "x" * 40}]) == 200 + TokenEstimate.text(
            len(json.dumps({"n": "x" * 40}))
        )


class TestMeasuredAnswers:
    @pytest.mark.parametrize("unmeasured", [answer(0, 50), answer(None, 50), answer(None, None)])
    def test_an_answer_with_no_input_count_is_not_a_measurement(self, unmeasured):
        conversation = [msg("user", "u" * 400), unmeasured]

        assert TokenEstimate.of(conversation) == 100

    def test_an_unmeasured_answer_after_a_measured_one_is_estimated_as_tail(self):
        conversation = [
            answer(900, 100),
            msg("tool", "t" * 400),
            answer(0, 0, "a" * 40),
        ]

        assert TokenEstimate.of(conversation) == 1000 + 100 + 10

    def test_a_measured_answer_with_no_output_count_counts_its_input(self):
        assert TokenEstimate.of([answer(700, None)]) == 700

    def test_when_the_answer_is_last_nothing_is_added(self):
        assert TokenEstimate.of([msg("user", "u" * 4000), answer(3000, 10)]) == 3010

    def test_only_an_assistant_message_is_a_measurement(self):
        measured_tool = msg(
            "tool", "t" * 40, metrics=SimpleNamespace(input_tokens=500, output_tokens=5)
        )

        assert TokenEstimate.of([measured_tool]) == 10

    def test_the_tools_are_not_added_to_a_measured_conversation(self):
        # The server's input count already includes the tool schemas.
        assert TokenEstimate.of([answer(100, 1)], [{"n": "x" * 4000}]) == 101


class TestToolSchemas:
    def test_no_tools_or_an_empty_list_is_zero(self):
        assert TokenEstimate.tool_chars(None) == 0
        assert TokenEstimate.tool_chars([]) == 0

    def test_a_dict_is_counted_as_its_json(self):
        tool = {"name": "read", "description": "d" * 100}

        assert TokenEstimate.tool_chars([tool]) == TokenEstimate.text(len(json.dumps(tool)))

    def test_an_object_with_to_dict_is_counted_as_that_json(self):
        schema = {"name": "write", "parameters": {"path": "string"}}
        tool = SimpleNamespace(to_dict=lambda: schema, name="ignored", description="ignored" * 50)

        assert TokenEstimate.tool_chars([tool]) == TokenEstimate.text(len(json.dumps(schema)))

    def test_any_other_object_is_its_name_and_description(self):
        tool = SimpleNamespace(name="n" * 20, description="d" * 20)

        assert TokenEstimate.tool_chars([tool]) == 10

    def test_an_object_with_neither_name_nor_description_costs_nothing(self):
        assert TokenEstimate.tool_chars([SimpleNamespace()]) == 0

    def test_a_to_dict_that_is_not_callable_is_ignored(self):
        tool = SimpleNamespace(to_dict="nope", name="n" * 8, description="d" * 8)

        assert TokenEstimate.tool_chars([tool]) == 4

    def test_the_schemas_are_summed_before_rounding(self):
        tools = [
            SimpleNamespace(name="ab", description=""),
            SimpleNamespace(name="cd", description=""),
        ]

        assert TokenEstimate.tool_chars(tools) == 1
