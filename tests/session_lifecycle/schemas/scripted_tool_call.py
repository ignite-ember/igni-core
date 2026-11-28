"""One tool call the scripted model asks for."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ScriptedToolCall(BaseModel):
    """A function call the model makes: its name and its JSON arguments."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str = ""
