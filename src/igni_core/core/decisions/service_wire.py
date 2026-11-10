"""The decisions service's wire: what the backend sends it and what it answers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from igni_core.core.decisions.laya_wire import LayaQuestions, LayaResult
from igni_core.core.decisions.schemas import (
    DecisionLimits,
    DecisionQuestion,
    DecisionRequest,
    Priority,
    Strict,
)


class PredictCall(Strict):
    """One question set about one context, on one checkpoint — the Bypass guard's call."""

    state: dict[str, str]
    questions: list[DecisionQuestion] = Field(min_length=1, max_length=DecisionLimits.QUESTIONS)
    model: str
    priority: Priority = Priority.APPROVAL

    def laya_questions(self) -> LayaQuestions:
        return LayaQuestions.of(self.questions)


class PredictAnswer(BaseModel):
    result: LayaResult


class DecideCall(Strict):
    """An extension's decision, on Laya's general checkpoints, behind the approvals."""

    request: DecisionRequest


class DecideAnswer(BaseModel):
    #: Laya's result per context; the asker reads them into decisions.
    results: list[LayaResult]


class DecisionsFailure(BaseModel):
    """Why the service answered nothing."""

    error: Literal["unavailable", "unauthorized"]
    detail: str


__all__ = ["DecideAnswer", "DecideCall", "DecisionsFailure", "PredictAnswer", "PredictCall"]
