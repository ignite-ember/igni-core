"""One user turn the model must go on seeing."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

TurnFate = Literal["sent", "completed", "paused", "cancelled", "queued", "denied", "errored"]


class ExpectedTurn(BaseModel):
    """What the person typed, how it went out, when, and what became of its run."""

    text: str
    kind: Literal["message", "queued"]
    sent_at: float
    fate: TurnFate = "sent"
