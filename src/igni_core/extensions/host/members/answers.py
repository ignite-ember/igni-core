"""What members answer, or emit, when it is more than one backend value.

Each is read from what igni's RPC handlers return, in one place, so the
member that answers with it does not build the shape by hand.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from igni_core.core.tools.orchestrate_events import VisualizationDeltaEvent
from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.values import NotificationEntry


class Display(BaseModel):
    """``app.display``: how igni shows a chat."""

    verbose: bool
    output_style: str
    output_styles: list[str]
    color_theme: str
    show_reasoning: bool
    show_tool_calls: bool

    @classmethod
    def from_rpc(cls, config: dict[str, Any], styles: dict[str, Any]) -> Display:
        """``get_display_config`` and ``get_output_styles``, together."""
        return cls(
            verbose=bool(config.get("show_routing", False)),
            output_style=str(styles.get("active") or ""),
            output_styles=[str(s.get("name", "")) for s in styles.get("styles") or []],
            color_theme=str(config.get("color_theme", "auto")),
            show_reasoning=bool(config.get("show_reasoning", False)),
            show_tool_calls=bool(config.get("show_tool_calls", True)),
        )


class Connection(BaseModel):
    """``app.connection``: whether igni reaches the cloud and has a model."""

    cloud_connected: bool
    model_configured: bool

    @classmethod
    def from_status(cls, status: dict[str, Any]) -> Connection:
        return cls(
            cloud_connected=bool(status.get("cloud_connected")),
            model_configured=bool(status.get("model_configured", True)),
        )


class AppStateChange(BaseModel):
    """``app.state.changed``: one of an extension's own keys, set or removed."""

    key: str
    value: str | None


class SessionSwitched(BaseModel):
    """The session a switch landed on, and the window that shows it."""

    session_id: str
    window: str | None


class ContextUsage(BaseModel):
    """``chat.context``: the context's size, its limit, and what fills it."""

    tokens: int
    max: int
    #: ``get_context_breakdown``, as the backend gives it.
    breakdown: Any


class CompactResult(BaseModel):
    """``chat.compact``: the session the history moved to, and its summary."""

    session_id: str
    new_session_id: str
    summary: str

    @classmethod
    def from_rpc(cls, session_id: str, cleared: Any) -> CompactResult | None:
        """``compact_if_needed``'s answer; nothing when nothing was compacted."""
        if not cleared:
            return None
        return cls(
            session_id=session_id,
            new_session_id=str(cleared.get("new_session_id", "")),
            summary=str(cleared.get("summary", "")),
        )


class PlanSnapshot(BaseModel):
    """``plan.latest``: the newest plan, its history, tasks and state."""

    latest: str
    history: list[Any]
    tasks: list[Any]
    state: str

    @classmethod
    def from_rpc(cls, found: dict[str, Any]) -> PlanSnapshot:
        return cls(
            latest=str(found.get("latest", "")),
            history=list(found.get("history") or []),
            tasks=list(found.get("tasks") or []),
            state=str(found.get("state", "")),
        )


class RpcInfo(BaseModel):
    """An RPC handler's ``info`` answer, which says a refusal rather than raising it."""

    type: str = ""
    text: str = ""

    @classmethod
    def from_rpc(cls, info: Any) -> RpcInfo:
        if not isinstance(info, dict):
            return cls()
        kind = info.get("type")
        return cls(type=kind if isinstance(kind, str) else "", text=str(info.get("text") or ""))

    def refusal(self) -> str | None:
        """The refusal's sentence, when the answer is one."""
        if self.type != "error":
            return None
        return self.text or "refused"


class ScheduledTask(BaseModel):
    """One scheduled task, as far as running it now needs."""

    key: str
    #: As the backend has it: what ``execute_scheduled_task`` is handed back.
    description: Any


class ScheduledTasks(BaseModel):
    """``get_scheduled_tasks``, by id."""

    tasks: list[ScheduledTask]

    @classmethod
    def from_rpc(cls, found: Any) -> ScheduledTasks:
        return cls(
            tasks=[
                ScheduledTask(
                    key=str(t.get("id") or t.get("task_id")),
                    description=t.get("description", ""),
                )
                for t in found or []
            ]
        )

    def find(self, task_id: str) -> ScheduledTask | None:
        return next((t for t in self.tasks if t.key == task_id), None)


class NotificationListing(BaseModel):
    """``notifications_list``: the floor's rows, and who owns which."""

    rows: list[dict[str, Any]]

    @classmethod
    def from_rpc(cls, found: Any) -> NotificationListing:
        rows = found.get("notifications", []) if isinstance(found, dict) else []
        return cls(rows=list(rows))

    def visible_to(self, grants: HostGrants, extension_id: str) -> list[NotificationEntry]:
        """Every row with ``notifications``; otherwise the extension's own."""
        entries = [NotificationEntry.from_row(r) for r in self.rows]
        if grants.allows("notifications"):
            return entries
        return [n for n in entries if n.source == extension_id]

    def foreign_ids(self, ids: list[Any], extension_id: str) -> list[Any]:
        """The ``ids`` that are not the extension's own rows. Compared as
        the rows carry them, not as strings."""
        own = {r.get("id") for r in self.rows if r.get("source") == extension_id}
        return [i for i in ids if i not in own]


class ExtensionViewEvent(VisualizationDeltaEvent):
    """A json-render block an extension puts in the chat: igni's own
    ``visualization_delta``, final, labelled with its title and source."""

    title: str
    source: str


__all__ = [
    "AppStateChange",
    "CompactResult",
    "Connection",
    "ContextUsage",
    "Display",
    "ExtensionViewEvent",
    "NotificationListing",
    "PlanSnapshot",
    "RpcInfo",
    "ScheduledTask",
    "ScheduledTasks",
    "SessionSwitched",
]
