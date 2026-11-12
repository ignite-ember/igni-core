"""The visualization action bus's typed action, broadcast and echo."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.backend.schemas_visualization import (
    VisualizationAction,
    VisualizationActionDispatchedEvent,
    VisualizationActionResult,
)


def test_an_action_without_params_has_an_empty_dict_of_its_own():
    first, second = VisualizationAction(action="a"), VisualizationAction(action="b")
    assert first.params == {} and second.params == {}
    assert first.params is not second.params


def test_an_action_cannot_be_changed_once_made():
    action = VisualizationAction(action="select", params={"row": 1})
    with pytest.raises(ValidationError):
        action.action = "other"  # type: ignore[misc]


def test_nested_json_params_are_kept_whole():
    params = {"rows": [1, {"id": "é", "ok": None}], "flag": True, "n": 1.5}
    assert VisualizationAction(action="é", params=params).params == params


@pytest.mark.parametrize("bad", [{"x": object()}, {"x": {1, 2}}])
def test_params_that_are_not_json_are_refused(bad):
    with pytest.raises(ValidationError):
        VisualizationAction(action="a", params=bad)


def test_an_empty_action_name_is_accepted():
    assert VisualizationAction(action="").action == ""


def test_the_broadcast_names_its_channel_and_carries_the_action_and_params():
    event = VisualizationActionDispatchedEvent.of(
        VisualizationAction(action="select", params={"row": 3})
    )
    assert event.channel == "visualization_action_dispatched"
    assert event.to_payload() == {"action": "select", "params": {"row": 3}}


def test_the_echo_confirms_with_the_same_action_and_params():
    result = VisualizationActionResult.echo(VisualizationAction(action="select", params={"r": [1]}))
    assert result.model_dump() == {"ok": True, "action": "select", "params": {"r": [1]}}


def test_an_echo_of_an_action_without_params_has_empty_params():
    assert VisualizationActionResult.echo(VisualizationAction(action="a")).params == {}
