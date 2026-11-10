"""Typed schemas for the json-render visualization action bus.

The action a view dispatches, the broadcast that announces it, and
the result echoed back to the frontend.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from igni_core.core.broadcast_schema import BroadcastEvent


class VisualizationAction(BaseModel):
    """One user-driven component action: its name and the JSON params
    the view attached to it."""

    model_config = ConfigDict(frozen=True)

    action: str
    params: dict[str, JsonValue] = Field(default_factory=dict)


class VisualizationActionDispatchedEvent(BroadcastEvent):
    """The ``visualization_action_dispatched`` broadcast, so log panels
    and dev tools can observe component interactions."""

    channel: Literal["visualization_action_dispatched"] = "visualization_action_dispatched"
    action: str = Field(description="Name of the action the user triggered in the visualization.")
    params: dict[str, JsonValue] = Field(
        description="Parameters the visualization attached to the action."
    )

    @classmethod
    def of(cls, entry: VisualizationAction) -> VisualizationActionDispatchedEvent:
        return cls(action=entry.action, params=entry.params)


class VisualizationActionResult(BaseModel):
    """Confirmation that a visualization action was dispatched.

    Echoes the action and its parameters, e.g. to show "you clicked X"
    in the conversation."""

    ok: bool = Field(description="`true` when the action was dispatched.")
    action: str = Field(description="Name of the action.")
    params: dict[str, JsonValue] = Field(
        default_factory=dict, description="Parameters attached to the action."
    )

    @classmethod
    def echo(cls, entry: VisualizationAction) -> VisualizationActionResult:
        return cls(ok=True, action=entry.action, params=entry.params)
