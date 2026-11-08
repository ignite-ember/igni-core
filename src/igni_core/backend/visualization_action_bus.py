"""Visualization action bus: a bounded history of FE-side json-render
component interactions, each one announced on the session broadcast bus.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from igni_core.backend.schemas_visualization import (
    VisualizationAction,
    VisualizationActionDispatchedEvent,
    VisualizationActionResult,
)

if TYPE_CHECKING:
    from igni_core.core.session import Session


class VisualizationActionBus:
    """Ring buffer + broadcast fan-out for FE-side json-render
    component interactions.

    32-entry ring so a chatty UI (e.g. a Slider firing on every
    drag tick) doesn't grow forever. Generous for the "one-off
    action after the user reviews a card" use case.
    """

    def __init__(self, session: Session, max_entries: int = 32) -> None:
        self._session = session
        self._actions: deque[VisualizationAction] = deque(maxlen=max_entries)

    @property
    def actions(self) -> list[VisualizationAction]:
        """The current ring (newest last), as a copy."""
        return list(self._actions)

    def dispatch(self, entry: VisualizationAction) -> VisualizationActionResult:
        """Record one user-driven component action on the ring and
        broadcast it on ``visualization_action_dispatched``. The
        broadcast bus isolates and logs subscriber failures."""
        self._actions.append(entry)
        self._session.broadcast(VisualizationActionDispatchedEvent.of(entry))
        return VisualizationActionResult.echo(entry)
