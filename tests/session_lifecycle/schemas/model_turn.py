"""One reply the scripted model streams for one request."""

from __future__ import annotations

from pydantic import BaseModel, Field

from tests.session_lifecycle.schemas.scripted_tool_call import ScriptedToolCall


class ModelTurn(BaseModel):
    """Text, reasoning and tool calls, streamed in chunks, with optional delays and a hold."""

    text: str = ""
    reasoning: str = ""
    tool_calls: list[ScriptedToolCall] = Field(default_factory=list)
    chunk_size: int = 16
    delay_before: float = 0.0
    chunk_delay: float = 0.0
    hold: str | None = Field(
        default=None, description="A gate the stream waits on after its first chunk."
    )
    error_status: int | None = Field(default=None, description="Answer with this HTTP error.")
    prompt_tokens: int = 100
    completion_tokens: int = 10
