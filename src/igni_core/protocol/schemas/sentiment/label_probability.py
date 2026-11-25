"""One label of a question Laya answered, and its probability."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LabelProbability(BaseModel):
    """One option, level, or ``yes`` / ``no``, and how likely Laya found it."""

    label: str = Field(description="The option, level, or `yes` / `no`.")
    probability: float = Field(description="Its probability, from 0 to 1.")


__all__ = ["LabelProbability"]
