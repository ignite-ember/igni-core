"""Each push channel is a typed model of its own, not a loose dict.

``core/broadcast_schema.py``. A channel's event says which fields it
carries and what they are, and refuses anything else, so the payload an
SDK is generated from is the payload the backend sends.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.backend.schemas_visualization import VisualizationActionDispatchedEvent
from igni_core.core.broadcast_schema import BroadcastEvent, RunScopedBroadcastEvent
from igni_core.core.session.schemas import (
    OutputStyleChangedEvent,
    PermissionModeChangedEvent,
    PlanDecidedEvent,
)
from igni_core.core.tools.plan import PlanModeEnteredEvent, PlanSubmittedEvent
from igni_core.core.tools.todo import TodosUpdatedEvent
from igni_core.core.tools.web_schemas import HttpRetryAttemptEvent, HttpRetrySucceededEvent


def _concrete_events() -> list[type[BroadcastEvent]]:
    found: list[type[BroadcastEvent]] = []
    todo = [BroadcastEvent]
    while todo:
        for sub in todo.pop().__subclasses__():
            todo.append(sub)
            if sub.__module__.startswith("igni_core.") and sub is not RunScopedBroadcastEvent:
                found.append(sub)
    return found


def test_the_real_channels_are_all_found():
    names = {event.__name__ for event in _concrete_events()}
    assert {
        TodosUpdatedEvent.__name__,
        PlanModeEnteredEvent.__name__,
        PlanSubmittedEvent.__name__,
        PlanDecidedEvent.__name__,
        OutputStyleChangedEvent.__name__,
        PermissionModeChangedEvent.__name__,
        HttpRetryAttemptEvent.__name__,
        HttpRetrySucceededEvent.__name__,
        VisualizationActionDispatchedEvent.__name__,
    } <= names


@pytest.mark.parametrize("event", _concrete_events(), ids=lambda e: e.__name__)
def test_a_channel_event_has_a_fixed_name_and_refuses_another(event: type[BroadcastEvent]):
    default = event.model_fields["channel"].default
    assert isinstance(default, str) and default
    with pytest.raises(ValidationError):
        event.model_validate({"channel": default + "_other"})


def test_a_todo_push_carries_the_list_and_names_its_channel():
    event = TodosUpdatedEvent(todos=[])
    assert event.channel == "todos_updated"
    assert event.to_payload() == {"todos": []}


def test_a_misspelt_field_on_a_real_event_is_refused():
    with pytest.raises(ValidationError):
        TodosUpdatedEvent(todos=[], todo=[])  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        OutputStyleChangedEvent(style="a", previous="b", extra="x")  # type: ignore[call-arg]


def test_a_field_of_the_wrong_type_is_refused():
    with pytest.raises(ValidationError):
        PlanDecidedEvent(run_id="r", decision="maybe")  # type: ignore[arg-type]


def test_a_real_event_is_frozen():
    event = OutputStyleChangedEvent(style="a", previous="b")
    with pytest.raises(ValidationError):
        event.style = "c"  # type: ignore[misc]


def test_a_plan_submission_is_stamped_with_the_run_it_belongs_to():
    stamped = PlanSubmittedEvent(plan="p").with_run_id("R-9")
    assert stamped.to_payload()["run_id"] == "R-9"
