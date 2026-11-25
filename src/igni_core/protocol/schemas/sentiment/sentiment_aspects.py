"""Every question Laya answered about a person's message, one field per question."""

from __future__ import annotations

from pydantic import BaseModel, Field

from igni_core.protocol.schemas.sentiment.sentiment_aspect import SentimentAspect


class SentimentAspects(BaseModel):
    """What Laya read in the message besides the verdict, for whoever learns from the turn."""

    feeling: SentimentAspect = Field(description="`positive`, `negative` or `neutral`.")
    gesture: SentimentAspect = Field(
        description="`gratitude`, `compliment`, `insult` or `neither`."
    )
    negative_reason: SentimentAspect = Field(
        description="What displeased the person: `wrong_or_incorrect`, `misunderstood_request`, "
        "`incomplete`, `ignored_instruction`, `unwanted_action`, `too_slow`, `wrong_length`, "
        "`tone`, `refused_or_gave_up` or `other`."
    )
    positive_reason: SentimentAspect = Field(
        description="What pleased the person: `solved_or_correct`, `fast`, `clear_explanation`, "
        "`beyond_expectations` or `other`."
    )
    repeat: SentimentAspect = Field(description="Whether the same problem happened again.")
    redo_requested: SentimentAspect = Field(
        description="Whether the person asks to redo or undo something."
    )
    task_done: SentimentAspect = Field(description="Whether the person says the task is done.")
    intensity: SentimentAspect = Field(description="`none`, `mild`, `strong` or `angry`.")


__all__ = ["SentimentAspects"]
