"""The plan models' published shapes: events, the hook payload, the verdicts.

The events are what a client sees on the wire; the hook payload is what
a ``PlanSubmit`` command hook reads on stdin; the decision and batch are
frozen values; the result envelopes refuse stray fields.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from igni_core.core.tools.plan.schemas import (
    PlanEnterResult,
    PlanExitResult,
    PlanModeEnteredEvent,
    PlanSubmitDecision,
    PlanSubmitHookPayload,
    PlanSubmittedEvent,
    PlanTaskBatch,
    PlanTaskInput,
)
from igni_core.core.tools.todo import TodoItemWire


class TestPlanModeEnteredEvent:
    def test_it_rides_the_mode_channel_with_its_own_kind(self):
        event = PlanModeEnteredEvent(mode="plan", source="agent")

        assert event.to_payload() == {
            "kind": "plan_mode_entered",
            "mode": "plan",
            "source": "agent",
            "reason": "",
        }
        assert event.channel == "permission_mode_changed"

    @pytest.mark.parametrize(
        "fields", [{"mode": "yolo", "source": "agent"}, {"mode": "plan", "source": "robot"}]
    )
    def test_an_unknown_mode_or_source_is_refused(self, fields):
        with pytest.raises(ValidationError):
            PlanModeEnteredEvent(**fields)

    def test_every_published_field_is_described(self):
        for name, field in PlanModeEnteredEvent.model_fields.items():
            if name != "channel":
                assert field.description, name


class TestPlanSubmittedEvent:
    def test_run_id_is_left_out_while_unknown(self):
        payload = json.loads(PlanSubmittedEvent(plan="p").model_dump_json())

        assert "run_id" not in payload
        assert payload["tasks"] == []

    def test_run_id_is_carried_once_known(self):
        event = PlanSubmittedEvent(plan="p", run_id="r1")

        assert event.to_payload()["run_id"] == "r1"

    def test_every_published_field_is_described(self):
        for name in ("run_id", "plan", "tasks"):
            assert PlanSubmittedEvent.model_fields[name].description, name


def test_the_hook_payload_is_the_three_fields_a_command_hook_reads():
    payload = PlanSubmitHookPayload(session_id="s-1", plan="# P", attempt=2)

    assert json.loads(payload.model_dump_json()) == {
        "session_id": "s-1",
        "plan": "# P",
        "attempt": 2,
    }


class TestFrozenValues:
    def test_a_decision_cannot_be_changed(self):
        decision = PlanSubmitDecision(allowed=True, plan="p")

        with pytest.raises(ValidationError):
            decision.allowed = False  # type: ignore[misc]
        assert PlanSubmitDecision(allowed=False).plan == ""
        assert PlanSubmitDecision(allowed=False).reason == ""

    def test_a_batch_cannot_be_changed(self):
        batch = PlanTaskBatch()

        assert (batch.tasks, batch.errors) == ([], [])
        with pytest.raises(ValidationError):
            batch.errors = ["x"]  # type: ignore[misc]


class TestTaskNormalising:
    def test_missing_none_and_blank_values_are_empty(self):
        for row in ({}, {"content": None}, {"content": "  "}):
            assert PlanTaskInput.model_validate(row).content == ""

    def test_a_nested_value_is_read_as_its_string_form(self):
        assert PlanTaskInput.model_validate({"content": {"a": 1}}).content == "{'a': 1}"

    def test_rows_may_come_from_a_generator(self):
        batch = PlanTaskInput.validate_batch(r for r in [{"content": "A"}, None, []])

        assert [t.content for t in batch.tasks] == ["A"]
        assert batch.errors == ["todos[1]: not a dict", "todos[2]: not a dict"]

    def test_a_loose_active_form_is_read_as_its_string_form(self):
        batch = PlanTaskInput.validate_batch([{"content": "A", "activeForm": ["x"]}])

        assert batch.errors == []
        assert batch.tasks[0].active_form == "['x']"


class TestResultEnvelopes:
    def test_the_enter_result_defaults(self):
        result = PlanEnterResult(ok=True, reply_text="r")

        assert (result.mode_flipped, result.mode_status, result.reason) == (False, "", "")
        assert (result.researcher_report, result.error) == ("", "")

    def test_a_stray_field_is_refused_on_both(self):
        with pytest.raises(ValidationError):
            PlanEnterResult(ok=True, reply_text="r", extra=1)  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            PlanExitResult(ok=True, reply_text="r", extra=1)  # type: ignore[call-arg]

    def test_the_task_count_is_published_with_the_result(self):
        rows = [TodoItemWire(content="A", status="pending", active_form="")]

        dumped = PlanExitResult(ok=True, reply_text="r", task_snapshot=rows).model_dump()

        assert dumped["task_count"] == 1
