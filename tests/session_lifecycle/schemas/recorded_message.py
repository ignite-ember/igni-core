"""One message of a request, as the model was shown it."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RecordedMessage(BaseModel):
    """A chat message's role, its text, and the tool calls it carries or answers."""

    role: str
    text: str = ""
    tool_call_names: list[str] = Field(default_factory=list)
    tool_call_ids: list[str] = Field(default_factory=list)
    tool_call_id: str | None = None

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> RecordedMessage:
        """One message of a chat-completions request body."""
        calls = raw.get("tool_calls") or []
        return cls(
            role=str(raw.get("role", "")),
            text=cls._text(raw.get("content")),
            tool_call_names=[str((c.get("function") or {}).get("name", "")) for c in calls],
            tool_call_ids=[str(c.get("id", "")) for c in calls],
            tool_call_id=raw.get("tool_call_id"),
        )

    @staticmethod
    def _text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        return ""
