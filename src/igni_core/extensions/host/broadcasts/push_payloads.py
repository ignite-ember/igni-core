"""The push payloads the translators read, parsed leniently at the seam.

igni's pushes are its private wire: a producer may leave a field out,
send ``None``, or add fields the host does not read. These readers take
any of that — ``extra="allow"``, no required field — and coerce each
field the way the translation always has, so a signal's value does not
change with the parsing:

* :data:`Text` is ``str(value)``: an explicit ``None`` reads ``"None"``,
  an absent field the default;
* :func:`_or` is ``str(value or fallback)``: anything falsy reads the
  fallback.

The producers' own models (``backend/schemas_push.py``) are stricter —
their fields are required — and would refuse the partial payloads a
producer is free to send, so they are not reused here.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def _or(fallback: str) -> BeforeValidator:
    return BeforeValidator(lambda v: str(v or fallback))


Text = Annotated[str, BeforeValidator(str)]
Blank = Annotated[str, _or("")]


class PushPayload(BaseModel):
    """Lenient: unknown fields kept, every field optional."""

    model_config = ConfigDict(extra="allow")


class EventPush(PushPayload):
    """A push that names its event in ``type`` (orchestration, workflows)."""

    type: Annotated[str, _or("event")] = "event"
    #: Passed on as sent: workflows name themselves however they like.
    name: Any = None


class SchedulerPush(PushPayload):
    task_id: Text = ""
    description: Text = ""
    result: Any = ""
    error: Any = ""
    event_type: Annotated[str, _or("event")] = "event"

    def outcome(self) -> str:
        """The result, or the error when there is none."""
        return str(self.result or self.error or "")


class VisualizationDelta(PushPayload):
    """A visualizer's spec, streamed; a view once ``final``."""

    type: Any = None
    final: Any = None
    spec_id: Text = ""
    spec_json: Annotated[str, _or("{}")] = Field("{}", alias="json")
    title: Blank = ""
    source: Annotated[str, _or("visualizer")] = "visualizer"

    def is_delta(self) -> bool:
        return self.type == "visualization_delta"

    def spec(self) -> Any:
        """The spec; empty when what was streamed is not JSON."""
        try:
            return json.loads(self.spec_json)
        except json.JSONDecodeError:
            return {}


class FileEditedPush(PushPayload):
    path: Text = ""


class SessionNamedPush(PushPayload):
    #: Empty when the push names no session: the push's own session, then.
    session_id: Blank = ""
    name: Text = ""


class PermissionModePush(PushPayload):
    mode: Text = ""
    previous: Text = ""


class ViewActionPush(PushPayload):
    action: Text = ""
    params: Annotated[Any, BeforeValidator(lambda v: v or {})] = Field(default_factory=dict)


class TodosPush(PushPayload):
    todos: Annotated[list[Any], BeforeValidator(lambda v: v if isinstance(v, list) else [])] = (
        Field(default_factory=list)
    )


class NotificationsPush(PushPayload):
    """The counts, under ``counts`` — or the whole payload, as older producers send it."""

    counts: dict[str, Any]

    @model_validator(mode="before")
    @classmethod
    def _counts(cls, data: Any) -> Any:
        if isinstance(data, dict):
            counts = data.get("counts")
            return {"counts": counts if isinstance(counts, dict) else data}
        return data


__all__ = [
    "EventPush",
    "FileEditedPush",
    "NotificationsPush",
    "PermissionModePush",
    "PushPayload",
    "SchedulerPush",
    "SessionNamedPush",
    "TodosPush",
    "ViewActionPush",
    "VisualizationDelta",
]
