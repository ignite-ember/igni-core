"""``RunWalker``: one persisted Agno run's messages become the chat's turns.

``test_plan_rehydrate`` reaches the walker only through the rebuilder, for
the plan card and the inline think tags. This file drives it directly: the
turn each role becomes, what is skipped, how the tool card is filled in,
and the character arithmetic behind the run's token badge.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.backend.schemas_history import (
    AssistantTurn,
    PlanTurn,
    StatsTurn,
    ThinkingTurn,
    ToolTurn,
    UserTurn,
)
from igni_core.backend.server_history_walker import RunWalker


def _walker(history_chars: int = 0, system_chars: int = 0) -> RunWalker:
    return RunWalker(run_id="r1", history_chars=history_chars, system_chars=system_chars)


def _msg(role: str, content: Any = "", **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(role=role, content=content, **extra)


def _plan_call(call_id: str = "c1", arguments: Any = None) -> dict[str, Any]:
    return {
        "id": call_id,
        "function": {
            "name": "exit_plan_mode",
            "arguments": json.dumps({"plan": "Do it", "tasks": [{"id": 1}]})
            if arguments is None
            else arguments,
        },
    }


class TestWhatEachRoleBecomes:
    def test_an_empty_run_has_no_turns(self) -> None:
        assert _walker().walk([]) == []

    def test_a_user_message(self) -> None:
        [turn] = _walker().walk([_msg("user", "hello", created_at=7)])

        assert isinstance(turn, UserTurn)
        assert (turn.content, turn.run_id, turn.created_at) == ("hello", "r1", 7)

    def test_an_assistant_message_with_sidecar_reasoning_thinks_before_it_replies(self) -> None:
        turns = _walker().walk(
            [_msg("assistant", "the answer", reasoning_content="working it out")]
        )

        assert [type(t) for t in turns] == [ThinkingTurn, AssistantTurn]
        assert turns[0].content == "working it out"
        assert turns[1].content == "the answer"

    def test_whitespace_reasoning_is_not_a_thinking_turn(self) -> None:
        turns = _walker().walk([_msg("assistant", "ok", reasoning_content="  \n ")])

        assert [type(t) for t in turns] == [AssistantTurn]

    def test_an_assistant_message_with_no_text_has_no_turn(self) -> None:
        assert _walker().walk([_msg("assistant", "   ")]) == []

    def test_inline_think_tags_split_the_reply_in_order(self) -> None:
        turns = _walker().walk([_msg("assistant", "before<think>hmm</think>after")])

        assert [(type(t), t.content) for t in turns] == [
            (AssistantTurn, "before"),
            (ThinkingTurn, "hmm"),
            (AssistantTurn, "after"),
        ]

    def test_a_cancelled_run_s_unclosed_think_tag_is_thinking_to_the_end(self) -> None:
        turns = _walker().walk([_msg("assistant", "a <think> never closed")])

        assert [(type(t), t.content) for t in turns] == [
            (AssistantTurn, "a"),
            (ThinkingTurn, "never closed"),
        ]

    def test_a_tool_result_is_a_tool_card_with_its_fields(self) -> None:
        [turn] = _walker().walk(
            [
                _msg(
                    "tool",
                    "file body",
                    tool_name="read_file",
                    tool_args={"file_name": "a.py", "n": 3},
                    tool_call_error=True,
                    created_at=9,
                )
            ]
        )

        assert isinstance(turn, ToolTurn)
        assert turn.tool_name == "read_file"
        assert turn.friendly_name == "Read"
        assert turn.args == "file_name=a.py n=3"
        assert turn.content == "file body"
        assert turn.is_error is True
        assert turn.is_rejected is False
        assert (turn.run_id, turn.created_at) == ("r1", 9)

    def test_a_tool_without_arguments_has_an_empty_summary_and_no_error(self) -> None:
        [turn] = _walker().walk([_msg("tool", "x", tool_name="grep", tool_args=None)])

        assert (turn.args, turn.is_error) == ("", False)  # type: ignore[union-attr]

    def test_a_tool_that_was_kept_from_running_is_rejected_not_failed(self) -> None:
        from igni_core.protocol.tool_error_conventions import ToolResultErrorDetector

        prefix = next(iter(ToolResultErrorDetector.default()._rejected_prefixes))
        [turn] = _walker().walk([_msg("tool", f"{prefix} nope", tool_name="run_shell_command")])

        assert turn.is_rejected is True  # type: ignore[union-attr]

    def test_the_system_message_is_never_a_turn(self) -> None:
        assert _walker().walk([_msg("system", "be good")]) == []


class TestWhatIsSkipped:
    def test_a_message_replayed_from_history(self) -> None:
        turns = _walker().walk([_msg("user", "old", from_history=True), _msg("user", "new")])

        assert [t.content for t in turns] == ["new"]  # type: ignore[union-attr]

    def test_an_unknown_role(self) -> None:
        turns = _walker().walk([_msg("critic", "psst"), _msg("user", "hi")])

        assert [type(t) for t in turns] == [UserTurn]

    def test_a_malformed_message_does_not_take_its_neighbours_with_it(self) -> None:
        bad = _msg("user", "x", created_at="not a number")

        turns = _walker().walk([_msg("user", "first"), bad, _msg("user", "last")])

        assert [t.content for t in turns] == ["first", "last"]  # type: ignore[union-attr]


class TestThePlanCard:
    def _walk(self, call: dict[str, Any], result_id: str = "c1", body: str = "ok") -> list[Any]:
        return _walker().walk(
            [
                _msg("assistant", "", tool_calls=[call]),
                _msg("tool", body, tool_name="exit_plan_mode", tool_call_id=result_id),
            ]
        )

    def test_the_result_of_exit_plan_mode_is_a_plan_in_place_of_a_tool_card(self) -> None:
        [plan] = self._walk(_plan_call())

        assert isinstance(plan, PlanTurn)
        assert (plan.plan, plan.tasks, plan.state, plan.run_id) == (
            "Do it",
            [{"id": 1}],
            "",
            "r1",
        )

    def test_arguments_given_as_a_dict(self) -> None:
        [plan] = self._walk(_plan_call(arguments={"plan": "  Dict plan  "}))

        assert isinstance(plan, PlanTurn)
        assert (plan.plan, plan.tasks) == ("Dict plan", [])

    def test_tasks_of_none_are_no_tasks(self) -> None:
        [plan] = self._walk(_plan_call(arguments={"plan": "P", "tasks": None}))

        assert plan.tasks == []  # type: ignore[union-attr]

    @pytest.mark.parametrize("plan_text", ["", "   \n"])
    def test_an_empty_plan_falls_back_to_a_tool_card(self, plan_text: str) -> None:
        [turn] = self._walk(_plan_call(arguments={"plan": plan_text}))

        assert isinstance(turn, ToolTurn)
        assert turn.tool_name == "exit_plan_mode"

    def test_arguments_that_are_not_json_leave_a_tool_card(self) -> None:
        [turn] = self._walk(_plan_call(arguments="{broken"))

        assert isinstance(turn, ToolTurn)

    def test_arguments_of_another_type_leave_a_tool_card(self) -> None:
        [turn] = self._walk(_plan_call(arguments=42))

        assert isinstance(turn, ToolTurn)

    def test_a_result_for_another_call_is_a_tool_card(self) -> None:
        [turn] = self._walk(_plan_call("c1"), result_id="other")

        assert isinstance(turn, ToolTurn)

    def test_a_call_with_no_id_cannot_be_matched(self) -> None:
        call = _plan_call()
        del call["id"]

        [turn] = self._walk(call, result_id="")

        assert isinstance(turn, ToolTurn)

    def test_other_tool_calls_and_junk_entries_are_ignored(self) -> None:
        walker = _walker()
        turns = walker.walk(
            [
                _msg(
                    "assistant",
                    "",
                    tool_calls=[{"id": "c9", "function": {"name": "grep", "arguments": "{}"}}],
                ),
                _msg("tool", "r", tool_name="grep", tool_call_id="c9"),
            ]
        )

        assert [type(t) for t in turns] == [ToolTurn]
        assert walker.plan_calls_in_run == {}

    def test_one_call_id_makes_one_plan_card(self) -> None:
        turns = _walker().walk(
            [
                _msg("assistant", "", tool_calls=[_plan_call()]),
                _msg("tool", "a", tool_name="exit_plan_mode", tool_call_id="c1"),
                _msg("tool", "b", tool_name="exit_plan_mode", tool_call_id="c1"),
            ]
        )

        assert [type(t) for t in turns] == [PlanTurn, ToolTurn]


class TestTheStatsBadge:
    def test_input_and_output_are_a_quarter_of_the_characters(self) -> None:
        walker = _walker(history_chars=100, system_chars=400)
        walker.walk([_msg("user", "u" * 40), _msg("assistant", "a" * 80)])

        stats = walker.finalize(SimpleNamespace(reasoning_tokens=5, duration=1.5))

        assert isinstance(stats, StatsTurn)
        # system 400 + the context before the run 100 + this run's user 40
        assert stats.input_tokens == (400 + 100 + 40) // 4
        assert stats.output_tokens == 20
        assert (stats.reasoning_tokens, stats.duration, stats.run_id) == (5, 1.5, "r1")

    def test_a_few_characters_still_count_as_one_token(self) -> None:
        walker = _walker()
        walker.walk([_msg("user", "abc"), _msg("assistant", "xy")])

        stats = walker.finalize(None)

        assert (stats.input_tokens, stats.output_tokens) == (1, 1)  # type: ignore[union-attr]

    def test_a_run_with_no_characters_has_no_badge(self) -> None:
        walker = _walker()
        walker.walk([])

        assert walker.finalize(SimpleNamespace(reasoning_tokens=9, duration=2.0)) is None

    def test_only_what_the_user_saw_as_a_reply_is_output(self) -> None:
        walker = _walker()
        walker.walk([_msg("assistant", "<think>" + "t" * 400 + "</think>abcd")])

        stats = walker.finalize(None)

        assert stats.output_tokens == 1  # type: ignore[union-attr]

    def test_characters_not_bytes(self) -> None:
        walker = _walker()
        walker.walk([_msg("assistant", "é" * 8)])

        assert walker.finalize(None).output_tokens == 2  # type: ignore[union-attr]

    @pytest.mark.parametrize(
        "metrics",
        [None, SimpleNamespace(reasoning_tokens="many", duration=1.0), object()],
    )
    def test_missing_or_malformed_metrics_are_zeros(self, metrics: Any) -> None:
        walker = _walker()
        walker.walk([_msg("user", "hello there")])

        stats = walker.finalize(metrics)

        assert isinstance(stats, StatsTurn)
        assert (stats.reasoning_tokens, stats.duration) == (0, 0.0)

    def test_metrics_with_none_fields_are_zeros(self) -> None:
        walker = _walker()
        walker.walk([_msg("user", "hello there")])

        stats = walker.finalize(SimpleNamespace(reasoning_tokens=None, duration=None))

        assert (stats.reasoning_tokens, stats.duration) == (0, 0.0)  # type: ignore[union-attr]


class TestTheCountersTheNextRunStartsFrom:
    def test_each_role_adds_what_the_model_saw(self) -> None:
        walker = _walker(history_chars=10)
        walker.walk(
            [
                _msg("user", "u" * 4),
                _msg("assistant", "a" * 6),
                _msg("tool", "t" * 8, tool_name="grep"),
            ]
        )

        assert walker.history_chars == 10 + 4 + 6 + 8

    def test_the_think_tags_count_toward_history_but_not_output(self) -> None:
        walker = _walker()
        walker.walk([_msg("assistant", "<think>x</think>y")])

        assert walker.history_chars == len("<think>x</think>y")
        assert walker.assistant_chars == 1

    def test_a_plan_result_counts_as_history(self) -> None:
        walker = _walker()
        walker.walk(
            [
                _msg("assistant", "", tool_calls=[_plan_call()]),
                _msg("tool", "x" * 12, tool_name="exit_plan_mode", tool_call_id="c1"),
            ]
        )

        assert walker.history_chars == 12

    def test_the_system_prompt_is_measured_once_and_kept_out_of_history(self) -> None:
        walker = _walker()
        walker.walk([_msg("system", "s" * 40), _msg("system", "z" * 999)])

        assert walker.system_chars == 40
        assert walker.history_chars == 0

    def test_a_system_prompt_already_measured_is_not_measured_again(self) -> None:
        walker = _walker(system_chars=40)
        walker.walk([_msg("system", "z" * 999)])

        assert walker.system_chars == 40

    def test_a_list_of_parts_is_counted_by_its_text_form(self) -> None:
        walker = _walker()
        walker.walk([_msg("user", ["a", "b"])])

        assert walker.history_chars == len(str(["a", "b"]))
