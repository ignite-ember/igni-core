"""``AgnoDecisionApplier``: what a person's yes or no does to a paused tool call."""

from __future__ import annotations

from agno.models.response import ToolExecution
from agno.run.requirement import RunRequirement

from igni_core.backend.agno_decision_applier import AgnoDecisionApplier
from igni_core.protocol import messages as msg


def _requirement(tool: str = "edit_file") -> RunRequirement:
    return RunRequirement(
        tool_execution=ToolExecution(tool_name=tool, tool_args={}, requires_confirmation=True)
    )


def _decision(action: str, requirement_id: str = "req-1") -> msg.HITLDecision:
    # Built unchecked: the wire keeps whatever literal a producer sent.
    return msg.HITLDecision.model_construct(requirement_id=requirement_id, action=action)


def test_confirm_lets_the_call_go_ahead():
    req = _requirement()

    result = AgnoDecisionApplier().apply(_decision("confirm"), req)

    assert result.ok
    assert req.tool_execution.confirmed is True


def test_reject_tells_the_model_which_call_the_person_declined():
    req = _requirement("edit_file")

    result = AgnoDecisionApplier().apply(_decision("reject"), req)

    assert result.ok
    assert req.tool_execution.confirmed is False
    assert req.confirmation_note == "Blocked: the person declined 'edit_file'."


def test_an_unknown_action_is_refused_and_names_the_action_and_the_requirement(caplog):
    req = _requirement()

    result = AgnoDecisionApplier().apply(_decision("maybe", "req-9"), req)

    assert not result.ok
    assert result.reason == "Unknown HITL action 'maybe' for requirement req-9"
    assert req.tool_execution.confirmed is None
    assert "Unknown HITL action 'maybe'" in caplog.text


def test_an_empty_action_is_refused_too():
    assert not AgnoDecisionApplier().apply(_decision(""), _requirement()).ok


class _Exploding:
    tool_execution = None

    def confirm(self):
        raise RuntimeError("cannot confirm")

    def reject(self, note=None):
        raise RuntimeError("cannot reject")


def test_a_failing_confirm_is_a_failed_result_not_an_exception():
    result = AgnoDecisionApplier().apply(_decision("confirm", "req-2"), _Exploding())

    assert not result.ok
    assert result.reason == "Failed to confirm requirement req-2: cannot confirm"


def test_a_failing_reject_is_a_failed_result_not_an_exception():
    result = AgnoDecisionApplier().apply(_decision("reject", "req-3"), _Exploding())

    assert not result.ok
    assert result.reason == "Failed to reject requirement req-3: cannot reject"


class _NoTool:
    tool_execution = None

    def __init__(self) -> None:
        self.note: str | None = None

    def reject(self, note=None):
        self.note = note


def test_a_requirement_without_a_tool_still_gets_a_declined_note():
    req = _NoTool()

    result = AgnoDecisionApplier().apply(_decision("reject"), req)

    assert result.ok
    assert req.note == "Blocked: the person declined this call."
