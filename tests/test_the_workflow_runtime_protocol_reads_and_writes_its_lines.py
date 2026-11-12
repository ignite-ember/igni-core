"""The wire protocol with ``workflow_runtime.mjs``: parsing stdout lines, the
stdin messages, and the record that is logged and pushed."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from igni_core.backend.schemas_workflow_events import (
    AgentRequestEvent,
    AgentResponseMessage,
    WorkflowCancelCommand,
    WorkflowCompletedEvent,
    WorkflowEventRecord,
    WorkflowFailedEvent,
    WorkflowMetaEvent,
    WorkflowProgressEvent,
    WorkflowRuntimeEventReader,
)
from igni_core.backend.schemas_workflows import WorkflowRunIdentity, WorkflowRunPhase

Reader = WorkflowRuntimeEventReader


def _line(type_: Any = "log", payload: Any = None, **top: Any) -> str:
    body: dict[str, Any] = {"ts": 1700, "run_id": "wf_x", "seq": 0, "type": type_}
    if payload is not None:
        body["payload"] = payload
    body.update(top)
    return json.dumps({k: v for k, v in body.items() if v is not ...})


# ── which event a line becomes ────────────────────────────────────


@pytest.mark.parametrize(
    ("type_", "payload", "expected"),
    [
        ("workflow_meta", {"meta": {"name": "n"}}, WorkflowMetaEvent),
        ("agent_request", {"id": "a"}, AgentRequestEvent),
        ("workflow_completed", {}, WorkflowCompletedEvent),
        ("workflow_failed", {}, WorkflowFailedEvent),
        ("phase_start", {"title": "t"}, WorkflowProgressEvent),
        ("log", {"message": "m"}, WorkflowProgressEvent),
        ("something_new", {}, WorkflowProgressEvent),
    ],
)
def test_a_line_becomes_the_event_its_type_names_and_any_other_is_progress(
    type_, payload, expected
):
    event = Reader.parse(_line(type_, payload))
    assert type(event) is expected
    assert event.type == type_
    assert (event.ts, event.run_id, event.seq) == (1700, "wf_x", 0)


def test_a_meta_event_reads_when_to_use_from_the_wire_name_and_the_path():
    event = Reader.parse(
        _line("workflow_meta", {"meta": {"name": "n", "whenToUse": "when"}, "path": "p.mjs"})
    )
    assert event.payload.meta.when_to_use == "when"
    assert event.payload.path == "p.mjs"
    assert event.payload_dump()["meta"]["whenToUse"] == "when"


@pytest.mark.parametrize(
    "line",
    [
        _line("log", seq=-1),
        _line("log", extra_key=1),
        _line(3),
        _line(None),
        _line("workflow_meta", {"meta": {"name": "n", "bogus": 1}}),
        _line("agent_request", {"prompt": "no id"}),
        "[1]",
        "null",
        "not json",
    ],
)
def test_a_line_that_is_not_an_event_is_refused(line):
    with pytest.raises(ValidationError):
        Reader.parse(line)


def test_a_line_without_a_type_is_refused():
    body = json.loads(_line())
    del body["type"]
    with pytest.raises(ValidationError):
        Reader.parse(json.dumps(body))


@pytest.mark.parametrize("raw", ["", "   \n", "{broken", "[1]", _line("log", seq=-1)])
def test_a_blank_or_malformed_line_is_skipped_not_raised(raw):
    assert Reader.parse_line(raw) is None


def test_a_good_line_with_surrounding_whitespace_is_parsed():
    event = Reader.parse_line(f"  {_line('log', {'message': 'm'})}\n")
    assert isinstance(event, WorkflowProgressEvent)


def test_a_refusal_names_the_kind_of_event_it_came_from():
    with pytest.raises(ValidationError) as caught:
        Reader.parse(_line("workflow_meta", {"meta": {"name": "n", "bogus": 1}}))
    assert Reader.failed_type(caught.value) == "meta"
    with pytest.raises(ValidationError) as caught:
        Reader.parse(_line("log", seq=-1))
    assert Reader.failed_type(caught.value) == "progress"


def test_a_refusal_before_any_kind_was_chosen_names_none():
    with pytest.raises(ValidationError) as caught:
        Reader.parse(_line(3))
    assert Reader.failed_type(caught.value) is None


# ── an agent() call the script waits on ───────────────────────────


def test_an_agent_request_takes_defaults_and_a_ten_minute_timeout():
    payload = Reader.parse(_line("agent_request", {"id": "a1"})).payload
    assert (payload.prompt, payload.label, payload.phase) == ("", None, None)
    assert payload.timeout_seconds == 600
    assert payload.timeout == 600
    assert payload.rejected_because is None


@pytest.mark.parametrize("prompt", ["text", ["a", {"role": "user"}], {"role": "user"}])
def test_an_agent_request_prompt_may_be_text_a_list_or_a_message(prompt):
    assert Reader.parse(_line("agent_request", {"id": "a", "prompt": prompt})).payload.prompt == (
        prompt
    )


def test_an_agent_request_takes_null_label_and_phase_and_a_numeric_id_as_text():
    payload = Reader.parse(
        _line("agent_request", {"id": 7, "label": None, "phase": None, "x": 1})
    ).payload
    assert payload.id == "7"
    assert payload.label is None
    assert payload.model_extra == {"x": 1}


@pytest.mark.parametrize("seconds", [0, -5, -0.5])
def test_a_timeout_of_zero_or_less_means_no_limit(seconds):
    payload = Reader.parse(_line("agent_request", {"id": "a", "timeout_seconds": seconds})).payload
    assert payload.timeout is None


def test_a_positive_timeout_is_the_seconds_asked_for():
    payload = Reader.parse(_line("agent_request", {"id": "a", "timeout_seconds": 2.5})).payload
    assert payload.timeout == 2.5


def test_a_malformed_request_with_an_id_is_kept_with_the_reason_it_was_refused():
    event = Reader.parse(_line("agent_request", {"id": 5, "timeout_seconds": "soon"}))
    assert isinstance(event, AgentRequestEvent)
    assert event.payload.id == "5"
    assert "timeout_seconds" in (event.payload.rejected_because or "")
    # the reason is for the backend; it is not forwarded to the log or the window
    assert "rejected_because" not in event.payload_dump()


# ── terminal events ───────────────────────────────────────────────


def test_a_completed_event_without_a_payload_is_a_plain_completion():
    event = Reader.parse(_line("workflow_completed"))
    assert event.phase is WorkflowRunPhase.COMPLETED
    assert event.payload.model_dump() == {"status": "completed", "duration_ms": 0, "result": None}


def test_a_completed_event_with_status_cancelled_is_a_cancellation():
    event = Reader.parse(_line("workflow_completed", {"status": "cancelled"}))
    assert event.phase is WorkflowRunPhase.CANCELLED


def test_a_failed_event_is_a_failure_and_defaults_its_text():
    event = Reader.parse(_line("workflow_failed"))
    assert event.phase is WorkflowRunPhase.FAILED
    assert event.payload.model_dump() == {"error": "", "stack": "", "duration_ms": 0}


def test_a_payload_dump_is_what_the_runtime_sent_unknown_keys_included():
    sent = {"error": "e", "custom": {"a": [1]}}
    assert Reader.parse(_line("workflow_failed", sent)).payload_dump() == sent
    assert Reader.parse(_line("workflow_completed", {})).payload_dump() == {}


def test_a_progress_payload_is_forwarded_untouched_as_a_copy():
    event = Reader.parse(_line("log", {"message": "é", "n": [1, 2]}))
    assert event.payload_dump() == {"message": "é", "n": [1, 2]}
    assert event.payload_dump() is not event.payload


# ── stdin ─────────────────────────────────────────────────────────


def test_the_cancel_command_serialises_as_type_cancel():
    assert WorkflowCancelCommand().model_dump() == {"type": "cancel"}


def test_a_success_reply_carries_the_result_and_no_error():
    reply = AgentResponseMessage.success("a1", {"k": [1]})
    assert reply.model_dump() == {
        "type": "agent_response",
        "id": "a1",
        "ok": True,
        "result": {"k": [1]},
        "error": None,
    }


def test_a_refusal_reply_carries_the_error_and_no_result():
    reply = AgentResponseMessage.refusal("a1", "no")
    assert reply.model_dump() == {
        "type": "agent_response",
        "id": "a1",
        "ok": False,
        "result": None,
        "error": "no",
    }


# ── the record that is logged and pushed ──────────────────────────

IDENTITY = WorkflowRunIdentity(workflow_run_id="wf_abc", name="deploy")


def test_a_record_copies_the_run_identity_and_the_events_own_fields():
    event = Reader.parse(_line("log", {"message": "m"}, seq=4))
    record = WorkflowEventRecord.from_event(IDENTITY, event)
    assert record.model_dump() == {
        "workflow_run_id": "wf_abc",
        "name": "deploy",
        "ts": 1700,
        "seq": 4,
        "type": "log",
        "payload": {"message": "m"},
    }


def test_a_record_of_a_typed_event_carries_the_payload_as_sent():
    event = Reader.parse(_line("workflow_failed", {"error": "e", "extra": 1}))
    assert WorkflowEventRecord.from_event(IDENTITY, event).payload == {"error": "e", "extra": 1}


@pytest.mark.parametrize(
    ("phase", "exit_code", "type_", "status"),
    [
        (WorkflowRunPhase.FAILED, 2, "workflow_failed", "failed"),
        (WorkflowRunPhase.COMPLETED, 0, "workflow_completed", "completed"),
        (WorkflowRunPhase.CANCELLED, -15, "workflow_completed", "cancelled"),
        (WorkflowRunPhase.FAILED, None, "workflow_failed", "failed"),
    ],
)
def test_the_event_igni_adds_when_the_process_ends_unreported(phase, exit_code, type_, status):
    record = WorkflowEventRecord.synthetic(IDENTITY, phase, exit_code)
    assert record.seq == -1
    assert record.type == type_
    assert record.payload == {"status": status, "exit_code": exit_code, "synthetic": True}
    assert (record.workflow_run_id, record.name) == ("wf_abc", "deploy")
    assert record.ts > 1_600_000_000_000


@pytest.mark.parametrize("phase", [WorkflowRunPhase.RUNNING, WorkflowRunPhase.CANCELLING])
def test_no_final_event_is_made_for_a_run_that_is_still_live(phase):
    with pytest.raises(ValueError, match="not a terminal phase"):
        WorkflowEventRecord.synthetic(IDENTITY, phase, 0)


def test_a_record_refuses_a_field_it_does_not_name():
    fields = {"workflow_run_id": "w", "name": "n", "ts": 1, "seq": 0, "type": "log", "payload": {}}
    assert WorkflowEventRecord(**fields).seq == 0
    with pytest.raises(ValidationError):
        WorkflowEventRecord(**fields, surprise=1)
