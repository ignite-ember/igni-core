"""Views: a visualizer's finished spec shown, and an action dispatched from one."""

from __future__ import annotations

from typing import Any, ClassVar

from igni_core.extensions.host.broadcasts.push_payloads import ViewActionPush, VisualizationDelta
from igni_core.extensions.host.broadcasts.signal import Signal


class ViewShown(Signal):
    """``chat.views.shown``, and each row of ``chat.views.list``."""

    WIRE: ClassVar[str] = "chat.views.shown"

    view_id: str
    session_id: str
    title: str
    #: A json-render spec, as the visualizer wrote it.
    spec: Any
    source: str

    @classmethod
    def shown(cls, session_id: str, delta: VisualizationDelta) -> ViewShown:
        return cls(
            view_id=delta.spec_id,
            session_id=session_id,
            title=delta.title,
            spec=delta.spec(),
            source=delta.source,
        )

    @classmethod
    def from_payload(cls, session_id: str, payload: dict[str, Any]) -> ViewShown:
        """A ``visualization_delta`` payload, as pushed or as kept in the event log."""
        return cls.shown(session_id, VisualizationDelta.model_validate(payload))


class ViewAction(Signal):
    WIRE: ClassVar[str] = "chat.views.action"

    session_id: str
    action: str
    #: The action's parameters, as the view sent them.
    params: Any

    @classmethod
    def from_payload(cls, session_id: str, push: ViewActionPush) -> ViewAction:
        return cls(session_id=session_id, action=push.action, params=push.params)


__all__ = ["ViewAction", "ViewShown"]
