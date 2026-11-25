"""Laya's answer to one question about a person's message."""

from __future__ import annotations

from pydantic import BaseModel, Field

from igni_core.protocol.schemas.sentiment.label_probability import LabelProbability


class SentimentAspect(BaseModel):
    """The label Laya chose, and every label's probability, to threshold again later."""

    answer: str = Field(description="The label Laya chose.")
    probabilities: list[LabelProbability] = Field(
        description="Every label's probability, in the question's order."
    )


__all__ = ["SentimentAspect"]
