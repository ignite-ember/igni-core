"""One rule of a model script: what to answer while a user message says something."""

from __future__ import annotations

from pydantic import BaseModel, Field

from tests.session_lifecycle.schemas.model_turn import ModelTurn


class ScriptRule(BaseModel):
    """The turns answered, one per step, after a user message containing ``when``."""

    when: str
    turns: list[ModelTurn] = Field(default_factory=list)
    then: ModelTurn = Field(
        default_factory=lambda: ModelTurn(text="Done."),
        description="Answered for every step past the scripted turns.",
    )
