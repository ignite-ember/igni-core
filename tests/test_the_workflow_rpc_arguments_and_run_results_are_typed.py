"""The workflow RPC argument and response models, a run's phase and identity,
and what a failed start turns into at the RPC boundary."""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from igni_core.backend.schemas_workflows import (
    MetaProbeResult,
    WorkflowCancelOutcome,
    WorkflowCancelRequest,
    WorkflowCancelResponse,
    WorkflowListItem,
    WorkflowListRequest,
    WorkflowMeta,
    WorkflowMetaEnvelope,
    WorkflowPhase,
    WorkflowRunIdentity,
    WorkflowRunPhase,
    WorkflowRunRequest,
    WorkflowRunResponse,
    WorkflowStartResult,
)

# ── what a workflow advertises ────────────────────────────────────


def test_meta_reads_when_to_use_from_the_wire_name_and_from_the_python_name():
    wire = WorkflowMeta.model_validate({"name": "n", "whenToUse": "w"})
    python = WorkflowMeta(name="n", when_to_use="w")
    assert wire == python
    assert wire.when_to_use == "w"


def test_meta_emits_when_to_use_under_its_wire_name_and_defaults_the_rest():
    assert WorkflowMeta(name="n").model_dump() == {
        "name": "n",
        "description": "",
        "whenToUse": "",
        "phases": [],
    }


def test_meta_refuses_a_key_it_does_not_know():
    with pytest.raises(ValidationError):
        WorkflowMeta.model_validate({"name": "n", "bogus": 1})
    with pytest.raises(ValidationError):
        WorkflowMeta.model_validate({"name": "n", "phases": [{"title": "t", "bogus": 1}]})


def test_meta_needs_a_name():
    with pytest.raises(ValidationError):
        WorkflowMeta.model_validate({})


def test_a_phase_may_leave_its_detail_out():
    assert WorkflowPhase(title="t").model_dump() == {"title": "t", "detail": ""}


def test_the_listing_row_flattens_the_meta_and_the_path_and_goes_out_by_wire_name():
    envelope = WorkflowMetaEnvelope(
        meta=WorkflowMeta(
            name="deploy",
            description="d",
            when_to_use="before release",
            phases=[WorkflowPhase(title="build", detail="make it")],
        ),
        path=".igni/workflows/deploy.mjs",
    )
    assert envelope.to_list_item().model_dump() == {
        "name": "deploy",
        "description": "d",
        "whenToUse": "before release",
        "phases": [{"title": "build", "detail": "make it"}],
        "path": ".igni/workflows/deploy.mjs",
    }


def test_a_listing_row_refuses_extras_and_accepts_either_spelling_of_when_to_use():
    row = {"name": "n", "phases": [], "path": "/abs/n.mjs"}
    assert WorkflowListItem(**row, when_to_use="w").when_to_use == "w"
    assert WorkflowListItem.model_validate({**row, "whenToUse": "w"}).when_to_use == "w"
    with pytest.raises(ValidationError):
        WorkflowListItem(**row, surprise=1)


# ── RPC arguments ─────────────────────────────────────────────────


def test_a_list_request_ignores_whatever_the_caller_sends():
    assert WorkflowListRequest.model_validate({"session_id": "s", "x": 1}).model_dump() == {}


def test_a_run_request_needs_a_non_empty_name():
    for params in ({}, {"name": ""}):
        with pytest.raises(ValidationError):
            WorkflowRunRequest.model_validate(params)


@pytest.mark.parametrize("args", [None, {}, 0, ""])
def test_absent_or_empty_run_args_are_an_empty_object(args):
    assert WorkflowRunRequest.model_validate({"name": "n", "args": args}).args == {}
    assert WorkflowRunRequest.model_validate({"name": "n"}).args == {}


def test_run_args_keep_nested_json_unchanged():
    args = {"files": ["a", "b"], "opts": {"deep": {"n": 1.5, "ok": None}}, "name": "é"}
    assert WorkflowRunRequest.model_validate({"name": "n", "args": args}).args == args


def test_run_args_that_are_not_json_are_refused():
    with pytest.raises(ValidationError):
        WorkflowRunRequest.model_validate({"name": "n", "args": {"x": object()}})


def test_a_run_request_takes_a_number_as_text_and_ignores_keys_it_does_not_name():
    request = WorkflowRunRequest.model_validate({"name": 42, "session_id": "s", "x": 1})
    assert request.name == "42"
    assert request.model_dump() == {"name": "42", "args": {}}


def test_a_cancel_request_needs_a_run_id_and_takes_a_number_as_text():
    with pytest.raises(ValidationError):
        WorkflowCancelRequest.model_validate({"workflow_run_id": ""})
    with pytest.raises(ValidationError):
        WorkflowCancelRequest.model_validate({})
    assert WorkflowCancelRequest.model_validate({"workflow_run_id": 12}).workflow_run_id == "12"


@pytest.mark.parametrize(
    ("outcome", "cancelled"),
    [
        (WorkflowCancelOutcome.CANCELLED, True),
        (WorkflowCancelOutcome.NOT_LIVE, False),
        (WorkflowCancelOutcome.ALREADY_EXITED, False),
    ],
)
def test_the_answer_to_a_cancel_echoes_the_id_and_says_only_whether_it_was_cancelled(
    outcome, cancelled
):
    request = WorkflowCancelRequest(workflow_run_id="wf_1")
    assert request.answer(outcome).model_dump() == {
        "cancelled": cancelled,
        "workflow_run_id": "wf_1",
    }
    assert outcome.cancelled is cancelled


def test_the_run_and_cancel_responses_refuse_extra_keys():
    with pytest.raises(ValidationError):
        WorkflowRunResponse(workflow_run_id="w", name="n", surprise=1)
    with pytest.raises(ValidationError):
        WorkflowCancelResponse(cancelled=True, workflow_run_id="w", surprise=1)


# ── a run's phase and identity ────────────────────────────────────


@pytest.mark.parametrize(
    ("phase", "terminal"),
    [
        (WorkflowRunPhase.RUNNING, False),
        (WorkflowRunPhase.CANCELLING, False),
        (WorkflowRunPhase.COMPLETED, True),
        (WorkflowRunPhase.FAILED, True),
        (WorkflowRunPhase.CANCELLED, True),
    ],
)
def test_only_completed_failed_and_cancelled_are_terminal(phase, terminal):
    assert phase.is_terminal is terminal


@pytest.mark.parametrize("phase", ["completed", "failed", "cancelled"])
def test_a_terminal_phase_is_the_status_string_the_window_reads(phase):
    assert WorkflowRunPhase(phase).terminal_status == phase


@pytest.mark.parametrize("phase", [WorkflowRunPhase.RUNNING, WorkflowRunPhase.CANCELLING])
def test_a_live_phase_has_no_terminal_status(phase):
    with pytest.raises(ValueError, match=f"{phase.value} is not a terminal phase"):
        _ = phase.terminal_status


def test_a_new_identity_is_wf_and_twelve_hex_digits_and_never_repeats():
    first, second = WorkflowRunIdentity.new("deploy"), WorkflowRunIdentity.new("deploy")
    assert re.fullmatch(r"wf_[0-9a-f]{12}", first.workflow_run_id)
    assert first.name == "deploy"
    assert first.workflow_run_id != second.workflow_run_id


def test_an_identity_cannot_be_changed():
    identity = WorkflowRunIdentity.new("n")
    with pytest.raises(ValidationError):
        identity.name = "other"  # type: ignore[misc]


# ── a start, and what the RPC boundary does with a miss ──────────


def test_a_started_run_is_answered_with_its_id_and_name():
    identity = WorkflowRunIdentity(workflow_run_id="wf_1", name="deploy")
    result = WorkflowStartResult.started(identity)
    assert result.ok is True
    assert result.to_response().model_dump() == {"workflow_run_id": "wf_1", "name": "deploy"}


def test_a_workflow_that_is_not_found_raises_file_not_found_with_the_reason():
    result = WorkflowStartResult.not_found("no workflow named x")
    assert (result.ok, result.failure) == (False, "not_found")
    with pytest.raises(FileNotFoundError, match="no workflow named x"):
        result.to_response()


def test_a_missing_runtime_raises_a_runtime_error_with_the_reason():
    result = WorkflowStartResult.runtime_missing("node is not installed")
    assert (result.ok, result.failure) == (False, "runtime_missing")
    with pytest.raises(RuntimeError, match="node is not installed") as caught:
        result.to_response()
    assert not isinstance(caught.value, FileNotFoundError)


def test_a_failed_result_with_no_failure_code_still_raises_a_runtime_error():
    with pytest.raises(RuntimeError, match="odd"):
        WorkflowStartResult(ok=False, reason="odd").to_response()


# ── the discovery probe ───────────────────────────────────────────


def test_a_probe_is_ok_only_when_it_found_a_meta():
    assert MetaProbeResult(meta=WorkflowMeta(name="n")).ok is True
    assert MetaProbeResult().ok is False
    assert MetaProbeResult(reason="exited 1").ok is False
