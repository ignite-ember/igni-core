"""One message the client sent into a session."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

SentKind = Literal["message", "queued"]


class SentMessage(BaseModel):
    """What the person typed, how it went out, into which session, and when."""

    text: str
    kind: SentKind
    session_id: str
    request_id: str = ""
    sent_at: float
