"""Views: a visualizer's finished spec shown, and an action dispatched from one."""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import JsonValue

from igni_core.backend.schemas_visualization import VisualizationActionDispatchedEvent
from igni_core.core.session.event_log_schema import VisualizationDeltaPayload
from igni_core.extensions.host.signal_values import Signal

#: Where a view came from, when the card named no source: igni's own
#: visualizer.
DEFAULT_VIEW_SOURCE = "visualizer"


class ViewShown(Signal):
    """``chat.views.shown``, and each row of ``chat.views.list``."""

    WIRE: ClassVar[str] = "chat.views.shown"

    view_id: str
    session_id: str
    title: str
    #: A json-render spec, as the visualizer wrote it.
    spec: JsonValue
    source: str

    @classmethod
    def shown(cls, session_id: str, delta: VisualizationDeltaPayload) -> ViewShown:
        """A finished card, as the visualizer's own model describes it."""
        return cls(
            view_id=delta.spec_id,
            session_id=session_id,
            title=delta.title,
            spec=_spec(delta.spec_json),
            source=delta.source or DEFAULT_VIEW_SOURCE,
        )


class ViewAction(Signal):
    WIRE: ClassVar[str] = "chat.views.action"

    session_id: str
    action: str
    #: The action's parameters, as the view sent them.
    params: dict[str, JsonValue]

    @classmethod
    def dispatched(cls, session_id: str, event: VisualizationActionDispatchedEvent) -> ViewAction:
        """An action, as the broadcast that carried it describes it."""
        return cls(session_id=session_id, action=event.action, params=event.params)


def _spec(spec_json: str) -> JsonValue:
    """The spec the card carries; empty when what was streamed is not JSON."""
    try:
        return json.loads(spec_json)
    except json.JSONDecodeError:
        return {}


__all__ = ["ViewAction", "ViewShown"]
