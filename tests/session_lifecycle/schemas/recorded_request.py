"""One request the scripted model received."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from tests.session_lifecycle.schemas.recorded_message import RecordedMessage

RequestOutcome = Literal["streaming", "completed", "aborted", "errored"]


class RecordedRequest(BaseModel):
    """Exactly what the model was shown: messages, tools, parameters; and how it ended."""

    index: int
    received_at: float
    model: str = ""
    stream: bool = False
    messages: list[RecordedMessage] = Field(default_factory=list)
    tool_names: list[str] = Field(default_factory=list)
    body: str = Field(default="", description="The request body, verbatim JSON.")
    rule: str = Field(default="", description="The script rule that answered it.")
    step: int = 0
    outcome: RequestOutcome = "streaming"
    finished_at: float | None = None

    def user_texts(self) -> list[str]:
        """The text of every user message, in order."""
        return [m.text for m in self.messages if m.role == "user"]

    def mentions(self, needle: str) -> bool:
        """Whether any message's text contains ``needle``."""
        return any(needle in m.text for m in self.messages)

    @property
    def label(self) -> str:
        """The script rule and step that answered it, for reports."""
        return f"{self.rule or 'unscripted'} step {self.step}"
