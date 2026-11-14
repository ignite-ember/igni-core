"""``TurnOutcome`` / ``TurnFailure`` / ``TurnResult``: a run read once.

The message pipeline reads every Agno run output in one place. These pin
what that reading says about each kind of run and what the failures a turn
can end in tell the person.
"""

from __future__ import annotations

import pytest
from agno.metrics import RunMetrics
from agno.models.response import ToolExecution
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.requirement import RunRequirement

from igni_core.core.session.message_outcome import (
    FailureKind,
    OutcomeKind,
    TurnFailure,
    TurnOutcome,
    TurnResult,
)


def _run(**kwargs) -> RunOutput:
    return RunOutput(**kwargs)


class TestHowAStatusIsRead:
    @pytest.mark.parametrize(
        ("status", "kind"),
        [
            (RunStatus.completed, OutcomeKind.COMPLETED),
            (RunStatus.paused, OutcomeKind.PAUSED),
            (RunStatus.error, OutcomeKind.FAILED),
            (RunStatus.cancelled, OutcomeKind.COMPLETED),
            ("PAUSED", OutcomeKind.PAUSED),
            ("paused", OutcomeKind.PAUSED),
            ("Error", OutcomeKind.FAILED),
            ("something-unknown", OutcomeKind.COMPLETED),
            (None, OutcomeKind.COMPLETED),
        ],
    )
    def test_the_status_decides_the_kind(self, status, kind):
        assert TurnOutcome.from_run(_run(status=status)).kind is kind


class TestWhatARunSaid:
    def test_text_content_is_kept(self):
        assert TurnOutcome.from_run(_run(content="héllo 世界")).content == "héllo 世界"

    @pytest.mark.parametrize("content", [None, {"answer": 1}, ["a"], 7])
    def test_content_that_is_not_text_reads_as_empty(self, content):
        assert TurnOutcome.from_run(_run(content=content)).content == ""

    def test_input_tokens_come_from_the_metrics(self):
        run = _run(metrics=RunMetrics(input_tokens=1234))

        assert TurnOutcome.from_run(run).input_tokens == 1234

    def test_a_run_without_metrics_has_zero_input_tokens(self):
        assert TurnOutcome.from_run(_run(metrics=None)).input_tokens == 0


class TestWhatAPauseWaitsOn:
    def test_the_paused_tools_are_named_and_a_nameless_one_is_a_question_mark(self):
        run = _run(
            status=RunStatus.paused,
            tools=[
                ToolExecution(tool_name="run_shell_command", requires_confirmation=True),
                ToolExecution(tool_name=None, requires_confirmation=True),
                ToolExecution(tool_name="already_ran"),
            ],
        )

        assert TurnOutcome.from_run(run).paused_tools == ["run_shell_command", "?"]

    def test_a_run_with_no_tools_waits_on_none(self):
        assert TurnOutcome.from_run(_run(tools=None)).paused_tools == []

    def test_only_the_unanswered_requirements_are_listed(self):
        pending = RunRequirement(
            tool_execution=ToolExecution(tool_name="a", requires_confirmation=True)
        )
        answered = RunRequirement(
            tool_execution=ToolExecution(tool_name="b", requires_confirmation=True)
        )
        answered.confirm()

        outcome = TurnOutcome.from_run(
            _run(status=RunStatus.paused, requirements=[pending, answered])
        )

        assert outcome.requirements == [pending]

    def test_a_run_with_no_requirements_has_none(self):
        assert TurnOutcome.from_run(_run(requirements=None)).requirements == []


class TestTheFailuresATurnCanEndIn:
    def test_a_pause_names_its_tools_and_says_nobody_can_answer_it(self):
        failure = TurnFailure.paused(
            TurnOutcome(kind=OutcomeKind.PAUSED, paused_tools=["run_shell_command", "write"])
        )

        assert failure.kind is FailureKind.PAUSED
        assert failure.error_type == "paused"
        assert failure.message == (
            "Run paused for confirmation on ['run_shell_command', 'write'] but this session "
            "cannot resolve a pause; the tool did not run. Grant the tool up front "
            "(--auto-approve or a settings rule) or use a client that supports confirmations."
        )

    def test_a_pause_with_no_paused_tool_says_unknown_tool(self):
        failure = TurnFailure.paused(TurnOutcome(kind=OutcomeKind.PAUSED))

        assert "on ['unknown tool'] but" in failure.message

    def test_a_failed_run_tells_its_own_content(self):
        failure = TurnFailure.failed(TurnOutcome(kind=OutcomeKind.FAILED, content="rate limited"))

        assert (failure.kind, failure.message, failure.error_type) == (
            FailureKind.FAILED,
            "rate limited",
            "failed",
        )

    def test_a_failed_run_that_said_nothing_says_it_failed(self):
        failure = TurnFailure.failed(TurnOutcome(kind=OutcomeKind.FAILED))

        assert failure.message == "The run failed."

    def test_an_exception_is_told_by_its_message_and_class(self):
        failure = TurnFailure.from_exception(KeyError("missing"))

        assert failure.kind is FailureKind.ERROR
        assert failure.message == "'missing'"
        assert failure.error_type == "KeyError"

    def test_an_exception_with_no_message_has_an_empty_one(self):
        failure = TurnFailure.from_exception(RuntimeError())

        assert (failure.message, failure.error_type) == ("", "RuntimeError")


class TestWhatATurnProduced:
    def test_a_completed_turn_is_ok_and_carries_the_reply(self):
        result = TurnResult.completed("hello")

        assert result.ok
        assert (result.reply, result.failure) == ("hello", None)

    def test_a_failed_turn_is_not_ok_and_has_no_reply(self):
        failure = TurnFailure.from_exception(RuntimeError("x"))

        result = TurnResult.failed(failure)

        assert not result.ok
        assert (result.reply, result.failure) == ("", failure)
