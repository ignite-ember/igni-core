"""One message of a request, as the model was shown it."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RecordedMessage(BaseModel):
    """A chat message's role, its text, and the tool calls it carries or answers."""

    role: str
    text: str = ""
    tool_call_names: list[str] = Field(default_factory=list)
    tool_call_ids: list[str] = Field(default_factory=list)
    tool_call_id: str | None = None
