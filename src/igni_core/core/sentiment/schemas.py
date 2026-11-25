"""What Laya's answer about a person's message comes to."""

from __future__ import annotations

from pydantic import BaseModel

from igni_core.protocol.schemas.enums import SentimentKind
from igni_core.protocol.schemas.sentiment.sentiment_aspects import SentimentAspects


class SentimentVerdict(BaseModel):
    """A sentiment Laya is sure of, and how sure."""

    sentiment: SentimentKind
    """``positive`` or ``negative``."""
    confidence: float
    """The probability the verdict rests on."""
    aspects: SentimentAspects
    """Every question's answer and probabilities."""


__all__ = ["SentimentVerdict"]
