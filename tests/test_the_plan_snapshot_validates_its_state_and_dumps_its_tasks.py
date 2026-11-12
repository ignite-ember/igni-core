"""The plan panel's snapshot: its empty defaults, its four-word state and the
camelCase key a task row dumps."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.backend.schemas_plan import LatestPlanResult
from igni_core.core.tools.todo import TodoItemWire


def test_no_plan_submitted_is_an_empty_snapshot():
    snapshot = LatestPlanResult()
    assert snapshot.model_dump(mode="json") == {
        "latest": "",
        "history": [],
        "tasks": [],
        "state": "",
    }


@pytest.mark.parametrize("state", ["pending", "approved", "dismissed", ""])
def test_the_four_states_are_accepted(state):
    assert LatestPlanResult(state=state).state == state


@pytest.mark.parametrize("state", ["rejected", "PENDING", None])
def test_a_state_outside_the_vocabulary_is_refused(state):
    with pytest.raises(ValidationError):
        LatestPlanResult(state=state)


def test_task_rows_are_typed_and_dump_their_active_form_camel_case():
    snapshot = LatestPlanResult(
        tasks=[{"content": "Run tests", "status": "in_progress", "activeForm": "Running tests"}]
    )
    assert isinstance(snapshot.tasks[0], TodoItemWire)
    dumped = snapshot.model_dump(mode="json")
    assert dumped["tasks"] == [
        {"content": "Run tests", "status": "in_progress", "activeForm": "Running tests"}
    ]


def test_a_task_with_an_unknown_status_is_refused():
    with pytest.raises(ValidationError):
        LatestPlanResult(tasks=[{"content": "x", "status": "someday"}])


def test_earlier_plans_keep_their_order():
    snapshot = LatestPlanResult(latest="C", history=["A", "B"], state="pending")
    assert (snapshot.latest, snapshot.history, snapshot.state) == ("C", ["A", "B"], "pending")
