"""One row of ``ember_received_messages``: a message igni recorded receiving."""

from __future__ import annotations

from pydantic import BaseModel


class PersistedMessage(BaseModel):
    """A received message's text and how its run ended, as stored."""

    message_id: str
    text: str
    status: str
    interrupted_reason: str | None = None
