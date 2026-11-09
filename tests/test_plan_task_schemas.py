"""The plan task rows are typed end to end.

``exit_plan_mode(tasks=[...])`` arrives from the LLM as loose
JSON. This file pins the shape of that hop: what
:class:`PlanTaskInput` accepts, what it rejects and how it names
the rejection, the checklist row it turns into, and the wire
shape the ``plan_submitted`` broadcast carries.

The rejected-row messages are what the agent reads to correct
itself, so they are asserted exactly — not just "some error".
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.core.tools.plan import PlanTool
from igni_core.core.tools.plan.schemas import (
    PlanExitResult,
    PlanSubmittedEvent,
    PlanTaskInput,
)
from igni_core.core.tools.todo import TodoItemWire
from tests.session_coordinators import plan_session, wire_coordinators


class TestValidateBatch:
    def test_valid_rows_become_typed_tasks(self):
        batch = PlanTaskInput.validate_batch([{"content": "Step 1", "activeForm": "Doing step 1"}])
        assert batch.errors == []
        assert [task.content for task in batch.tasks] == ["Step 1"]
        assert [task.active_form for task in batch.tasks] == ["Doing step 1"]

    def test_no_rows_is_an_empty_batch(self):
        for rows in (None, []):
            batch = PlanTaskInput.validate_batch(rows)
            assert batch.tasks == []
            assert batch.errors == []

    def test_non_dict_row_is_named_by_index(self):
        batch = PlanTaskInput.validate_batch(["not a dict", 42])
        assert batch.tasks == []
        assert batch.errors == ["todos[0]: not a dict", "todos[1]: not a dict"]

    def test_empty_content_is_named_by_index(self):
        batch = PlanTaskInput.validate_batch([{"content": "OK"}, {"content": ""}])
        assert [task.content for task in batch.tasks] == ["OK"]
        assert batch.errors == ["todos[1]: empty content"]

    def test_whitespace_only_content_is_empty(self):
        batch = PlanTaskInput.validate_batch([{"content": "   "}])
        assert batch.tasks == []
        assert batch.errors == ["todos[0]: empty content"]

    def test_missing_content_is_empty_content(self):
        batch = PlanTaskInput.validate_batch([{"activeForm": "Doing something"}])
        assert batch.tasks == []
        assert batch.errors == ["todos[0]: empty content"]

    def test_content_is_stripped(self):
        batch = PlanTaskInput.validate_batch([{"content": "  padded  "}])
        assert batch.tasks[0].content == "padded"

    def test_numeric_content_becomes_its_string_form(self):
        """A numeric content is coerced, not rejected — the agent
        meant a task, and the row's shape is fine."""
        batch = PlanTaskInput.validate_batch([{"content": 5}])
        assert batch.errors == []
        assert batch.tasks[0].content == "5"

    def test_snake_case_active_form_is_accepted(self):
        batch = PlanTaskInput.validate_batch([{"content": "A", "active_form": "Doing A"}])
        assert batch.tasks[0].active_form == "Doing A"

    def test_camel_case_active_form_wins_over_snake_case(self):
        batch = PlanTaskInput.validate_batch(
            [{"content": "A", "activeForm": "Camel", "active_form": "Snake"}]
        )
        assert batch.tasks[0].active_form == "Camel"

    def test_unknown_keys_are_ignored(self):
        batch = PlanTaskInput.validate_batch([{"content": "A", "colour": "blue"}])
        assert batch.errors == []
        assert batch.tasks[0].content == "A"

    def test_status_is_ignored(self):
        """A plan enumerates work; it does not schedule it. A
        status the agent invented does not make the row invalid."""
        batch = PlanTaskInput.validate_batch([{"content": "A", "status": "bogus"}])
        assert batch.errors == []
        assert batch.tasks[0].content == "A"

    def test_a_bad_row_does_not_take_the_good_ones_with_it(self):
        batch = PlanTaskInput.validate_batch([{"content": "A"}, {"content": ""}, {"content": "B"}])
        assert [task.content for task in batch.tasks] == ["A", "B"]
        assert batch.errors == ["todos[1]: empty content"]

    def test_a_row_that_cannot_be_read_is_named_by_index(self):
        """A value that cannot even be turned into a string is a
        rejected row, not a crash."""

        class Unreadable:
            def __str__(self) -> str:
                raise ValueError("no string for you")

        batch = PlanTaskInput.validate_batch([{"content": Unreadable()}])
        assert batch.tasks == []
        assert batch.errors == ["todos[0]: invalid task row"]


class TestToWire:
    def test_row_becomes_a_pending_checklist_row(self):
        task = PlanTaskInput(content="Step 1", activeForm="Doing step 1")
        wire = task.to_wire()
        assert wire.content == "Step 1"
        assert wire.status == "pending"
        assert wire.active_form == "Doing step 1"

    def test_row_without_active_form_has_an_empty_one(self):
        assert PlanTaskInput(content="Step 1").to_wire().active_form == ""


class TestBroadcastPayload:
    def test_tasks_serialise_in_the_camel_case_wire_shape(self):
        event = PlanSubmittedEvent(
            plan="the plan",
            tasks=[TodoItemWire(content="A", status="pending", active_form="Doing A")],
        )
        payload = event.to_payload()
        assert payload["plan"] == "the plan"
        assert payload["tasks"] == [{"content": "A", "status": "pending", "activeForm": "Doing A"}]

    def test_a_plan_without_tasks_carries_an_empty_list(self):
        assert PlanSubmittedEvent(plan="the plan").to_payload()["tasks"] == []


class TestResultEnvelope:
    def test_snapshot_holds_typed_rows(self):
        result = PlanExitResult(
            ok=True,
            task_snapshot=[TodoItemWire(content="A", status="pending")],
            reply_text="Plan submitted.",
        )
        assert result.task_snapshot[0].content == "A"

    def test_task_count_is_derived_from_the_snapshot(self):
        result = PlanExitResult(
            ok=True,
            task_snapshot=[TodoItemWire(content="A", status="pending")],
            reply_text="Plan submitted.",
        )
        assert result.task_count == 1
        assert result.model_dump()["task_count"] == 1

    def test_snapshot_rejects_raw_dicts(self):
        """The contract is typed: a caller cannot smuggle a raw
        dict into the envelope."""
        with pytest.raises(ValidationError):
            PlanExitResult(ok=True, task_snapshot=[{"content": "A"}], reply_text="x")


@pytest.mark.asyncio
async def test_exit_plan_mode_applies_valid_rows_and_names_the_bad_one():
    """End to end through the tool: the good rows land in the
    todo store, the bad one comes back as a named error."""
    session = plan_session()
    tool = PlanTool(session)

    reply = await tool.exit_plan_mode(
        "the plan",
        tasks=[{"content": "A", "activeForm": "Doing A"}, {"content": ""}],
    )

    assert "Plan submitted" in reply
    assert "Tasks validation errors (ignored): todos[1]: empty content" in reply
    assert [item.content for item in session.todo_store.items] == ["A"]
    assert session.todo_store.items[0].active_form == "Doing A"
    assert session.todo_store.items[0].status == "pending"


@pytest.mark.asyncio
async def test_exit_plan_mode_broadcast_carries_typed_rows():
    session = plan_session()
    wire_coordinators(session)
    captured: list[tuple[str, dict]] = []
    session.register_broadcast_callback(lambda e: captured.append((e.channel, e.to_payload())))

    tool = PlanTool(session)
    await tool.exit_plan_mode("the plan", tasks=[{"content": "A", "activeForm": "Doing A"}])
    session.broadcast_bus.drain_post_run(run_id=None)

    payload = next(p for channel, p in captured if channel == "plan_submitted")
    assert payload["tasks"] == [{"content": "A", "status": "pending", "activeForm": "Doing A"}]
