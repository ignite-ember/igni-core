"""One frame the backend sent the client, as received."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class WireFrame(BaseModel):
    """A received message: its routing fields, when it arrived, and its JSON as sent."""

    type: str
    id: str = ""
    session_id: str = ""
    run_id: str = ""
    parent_run_id: str = ""
    channel: str = ""
    received_at: float
    data: dict[str, Any] = Field(default_factory=dict, description="The frame's JSON, verbatim.")

    @property
    def top_level(self) -> bool:
        """A frame of the session's own run, not a sub-agent's."""
        return not self.parent_run_id

    @property
    def text(self) -> str:
        return str(self.data.get("text") or "")
