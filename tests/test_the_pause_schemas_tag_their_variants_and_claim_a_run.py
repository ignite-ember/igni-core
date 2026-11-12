"""The pause schemas: the auto-decision words, the defaults of a handled pause,
the tagged queue variants and the RunPaused a sub-agent batch becomes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from igni_core.backend.schemas_pause import (
    ApplyDecisionResult,
    AutoDecision,
    MuxDone,
    MuxError,
    PauseHandleResult,
    PendingRequirement,
    SubagentDetails,
    SubagentPause,
    TeamStreamEvent,
)
from igni_core.core.hitl.models import DetailUpdate, PendingEntry, PendingHandle
from igni_core.protocol import messages as msg


def _handle(requirement_id: str, run_id: str, tool: str = "shell") -> PendingHandle:
    requirement = SimpleNamespace(
        tool_execution=SimpleNamespace(tool_name=tool, tool_args={"command": "ls"})
    )
    return PendingHandle(
        requirement_id=requirement_id,
        entry=PendingEntry(requirement=requirement, run_id=run_id, agent_path=["architect"]),
    )


def test_an_auto_decision_is_confirm_or_reject_and_round_trips_by_its_word():
    assert [d.value for d in AutoDecision] == ["confirm", "reject"]
    assert AutoDecision("confirm") is AutoDecision.CONFIRM
    assert AutoDecision("reject") is AutoDecision.REJECT
    assert AutoDecision.CONFIRM == "confirm"
    with pytest.raises(ValueError):
        AutoDecision("defer")


def test_a_pending_requirement_may_hold_neither_a_run_nor_a_request():
    pending = PendingRequirement(req=object())
    assert pending.run_id is None
    assert pending.request is None


def test_a_pending_requirement_keeps_the_request_its_window_was_shown():
    request = msg.HITLRequest(requirement_id="r1", tool_name="shell")
    pending = PendingRequirement(req=object(), run_id="run-1", request=request)
    assert pending.request is not None
    assert (pending.run_id, pending.request.requirement_id) == ("run-1", "r1")


def test_a_decision_result_has_no_reason_unless_it_failed():
    assert ApplyDecisionResult(ok=True).reason == ""
    failed = ApplyDecisionResult(ok=False, reason="boom")
    assert (failed.ok, failed.reason) == (False, "boom")


def test_a_handled_pause_is_empty_by_default():
    result = PauseHandleResult()
    assert (result.messages, result.auto_resolved, result.run_id) == ([], [], None)
    # Defaults are fresh lists, not one shared list.
    result.messages.append(msg.Info(text="x"))
    assert PauseHandleResult().messages == []


def test_every_queue_variant_carries_its_fixed_kind():
    assert TeamStreamEvent(event=1).kind == "event"
    assert SubagentPause().kind == "subagent_pause"
    assert SubagentDetails().kind == "subagent_details"
    assert MuxDone().kind == "done"
    assert MuxError(exc=ValueError("x")).kind == "error"


@pytest.mark.parametrize("variant", [TeamStreamEvent, SubagentPause, SubagentDetails, MuxDone])
def test_a_variant_refuses_another_variant_s_kind(variant):
    kwargs = {"event": 1} if variant is TeamStreamEvent else {}
    with pytest.raises(ValidationError):
        variant(kind="error", **kwargs)


def test_a_mux_error_holds_any_exception_object_as_it_is():
    class Odd(Exception):
        pass

    exc = Odd("odd")
    assert MuxError(exc=exc).exc is exc
    assert MuxError(exc="not an exception").exc == "not an exception"


def test_subagent_details_hold_the_coordinator_s_updates():
    updates = [DetailUpdate(requirement_id="r1", details="because")]
    assert SubagentDetails(updates=updates).updates == updates
    assert SubagentDetails().updates == []


def test_a_sub_agent_batch_becomes_one_run_paused_with_each_entry_s_request():
    batch = SubagentPause(entries=[_handle("r1", "sub-run-1"), _handle("r2", "sub-run-2", "edit")])
    paused = batch.run_paused()
    assert isinstance(paused, msg.RunPaused)
    assert [r.requirement_id for r in paused.requirements] == ["r1", "r2"]
    assert [r.tool_name for r in paused.requirements] == ["shell", "edit"]
    # Entries of several runs: the first one's run id alone is forwarded.
    assert paused.run_id == "sub-run-1"


def test_an_empty_sub_agent_batch_is_a_run_paused_with_no_run_and_no_requirements():
    paused = SubagentPause().run_paused()
    assert isinstance(paused, msg.RunPaused)
    assert paused.run_id == ""
    assert paused.requirements == []
