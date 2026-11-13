"""``PausePolicyResolver.settle``: chained pauses, the resume cap, unanswerable pauses.

The resolver is driven directly with a fake team and a fake evaluator, so the
loop's own branches are what is under test: what it continues, with which
session id, and when it stops.
"""

from __future__ import annotations

import logging
from typing import Any

from agno.models.response import ToolExecution
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.requirement import RunRequirement

from igni_core.core.config.permission_eval import PermissionDecision, PermissionOutcome
from igni_core.core.session.message_pause_policy import PausePolicyResolver


def _requirement(name: str = "run_shell_command", **args: Any) -> RunRequirement:
    return RunRequirement(
        tool_execution=ToolExecution(tool_name=name, tool_args=args, requires_confirmation=True)
    )


def _paused(*requirements: RunRequirement) -> RunOutput:
    return RunOutput(status=RunStatus.paused, requirements=list(requirements))


def _done() -> RunOutput:
    return RunOutput(status=RunStatus.completed, content="done")


class _Evaluator:
    def __init__(self, decision: PermissionDecision = PermissionDecision.ALLOW, reason: str = ""):
        self.decision = decision
        self.reason = reason
        self.asked: list[tuple[str, dict[str, Any]]] = []

    def evaluate_outcome(self, name: str, args: dict[str, Any]) -> PermissionOutcome:
        self.asked.append((name, args))
        return PermissionOutcome(decision=self.decision, reason=self.reason, source="mode")


class _Team:
    """Continues a run by handing back the next scripted output."""

    def __init__(self, *outputs: RunOutput) -> None:
        self._outputs = list(outputs)
        self.continued: list[dict[str, Any]] = []

    async def acontinue_run(self, **kwargs: Any) -> RunOutput:
        self.continued.append(kwargs)
        return self._outputs.pop(0) if len(self._outputs) > 1 else self._outputs[0]


def _resolver(evaluator: Any, session_id=lambda: "s-1") -> PausePolicyResolver:
    return PausePolicyResolver(evaluator_ref=lambda: evaluator, session_id_ref=session_id)


async def test_a_run_that_is_not_paused_is_returned_untouched():
    team = _Team(_done())
    run = _done()

    assert await _resolver(_Evaluator()).settle(team, run) is run
    assert team.continued == []


async def test_a_pause_with_no_requirements_is_left_as_it_is():
    team = _Team(_done())
    run = _paused()

    assert await _resolver(_Evaluator()).settle(team, run) is run
    assert team.continued == []


async def test_the_run_is_continued_non_streamed_on_the_live_session_id():
    live = {"id": "before"}
    resolver = _resolver(_Evaluator(), lambda: live["id"])

    team = _Team(_done())
    await resolver.settle(team, _paused(_requirement()))
    assert team.continued[0]["session_id"] == "before"
    assert team.continued[0]["stream"] is False

    live["id"] = "rotated"
    team = _Team(_done())
    await resolver.settle(team, _paused(_requirement()))
    assert team.continued[0]["session_id"] == "rotated"


async def test_each_pause_of_a_chain_is_settled_in_turn():
    evaluator = _Evaluator()
    first, second = _paused(_requirement(command="ls")), _paused(_requirement(command="pwd"))
    team = _Team(second, _done())

    settled = await _resolver(evaluator).settle(team, first)

    assert settled.content == "done"
    assert [c["run_response"] for c in team.continued] == [first, second]
    assert [args for _, args in evaluator.asked] == [{"command": "ls"}, {"command": "pwd"}]


async def test_a_run_that_keeps_pausing_is_cut_off_after_the_cap():
    class _Looping(_Team):
        async def acontinue_run(self, **kwargs: Any) -> RunOutput:
            self.continued.append(kwargs)
            return _paused(_requirement())

    team = _Looping()

    settled = await _resolver(_Evaluator()).settle(team, _paused(_requirement()))

    assert len(team.continued) == PausePolicyResolver.MAX_POLICY_RESUMES == 50
    assert settled.status is RunStatus.paused


async def test_an_evaluator_that_raises_leaves_the_pause_alone_and_says_so(caplog):
    class _Broken:
        def evaluate_outcome(self, name, args):
            raise RuntimeError("rules unreadable")

    team = _Team(_done())
    requirement = _requirement("write_file")
    run = _paused(requirement)

    with caplog.at_level(logging.WARNING):
        settled = await _resolver(_Broken()).settle(team, run)

    assert settled is run
    assert requirement.confirmation is None
    assert team.continued == []
    assert "permission evaluation failed for write_file" in caplog.text


async def test_a_pause_with_nothing_left_to_answer_is_left_as_it_is():
    """A requirement with no tool execution needs nothing from anyone."""
    evaluator = _Evaluator()
    team = _Team(_done())
    run = _paused(RunRequirement(tool_execution=None))  # type: ignore[arg-type]

    assert await _resolver(evaluator).settle(team, run) is run
    assert evaluator.asked == []
    assert team.continued == []


async def test_a_tool_without_args_is_asked_about_with_empty_args():
    evaluator = _Evaluator()
    team = _Team(_done())

    await _resolver(evaluator).settle(
        team,
        _paused(
            RunRequirement(tool_execution=ToolExecution(tool_name="t", requires_confirmation=True))
        ),
    )

    assert evaluator.asked == [("t", {})]


async def test_a_denial_is_rejected_with_its_reason_for_the_model_to_read():
    requirement = _requirement(command="rm -rf /")
    team = _Team(_done())

    await _resolver(_Evaluator(PermissionDecision.DENY, "rm is denied")).settle(
        team, _paused(requirement)
    )

    assert requirement.confirmation is False
    assert requirement.confirmation_note == "Blocked: rm is denied"


async def test_ask_level_verdicts_leave_the_pause_for_a_person():
    requirement = _requirement()
    team = _Team(_done())
    run = _paused(requirement)

    assert await _resolver(_Evaluator(PermissionDecision.ASK)).settle(team, run) is run
    assert requirement.confirmation is None
    assert team.continued == []
