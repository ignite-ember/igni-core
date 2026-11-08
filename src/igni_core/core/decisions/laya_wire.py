"""Laya's own shapes: the questions it is asked and the result it gives back.

A question goes to Laya as ``{"type", "instructions", "criteria"}``
(:class:`LayaQuestion`); a result comes back as answers per question,
token usage and the checkpoint the router picked (:class:`LayaResult`),
which reads itself back into a :class:`~igni_core.core.decisions.schemas.Decision`.
Laya says more than igni reads; the rest is ignored.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, RootModel

from igni_core.core.decisions.schemas import (
    Decision,
    DecisionAnswer,
    DecisionContext,
    DecisionQuestion,
    DecisionTooLong,
    QuestionKind,
    Strict,
)


class LayaQuestion(Strict):
    """One question in Laya's own shape."""

    type: QuestionKind
    instructions: str
    #: ``choice``: label → description (None when the label says it);
    #: ``score``: the levels, lowest first; ``noul``: none.
    criteria: dict[str, str | None] | list[str] | None = None

    @classmethod
    def of(cls, question: DecisionQuestion) -> LayaQuestion:
        if question.kind is QuestionKind.CHOICE:
            criteria: dict[str, str | None] | list[str] | None = {
                label: (text or None) for label, text in (question.options or {}).items()
            }
        elif question.kind is QuestionKind.SCORE:
            criteria = list(question.levels or [])
        else:
            criteria = None
        return cls(type=question.kind, instructions=question.question, criteria=criteria)


class LayaQuestions(RootModel[dict[str, LayaQuestion]]):
    """A question set by id, as Laya's router takes it."""

    @classmethod
    def of(cls, questions: list[DecisionQuestion]) -> LayaQuestions:
        return cls({q.id: LayaQuestion.of(q) for q in questions})

    def wire(self) -> dict[str, Any]:
        """What the router is handed: a ``noul`` question carries no criteria."""
        return self.model_dump(mode="json", exclude_none=True)


class LayaBatchItem(Strict):
    """One context and its questions, as an item of the router's batch."""

    state: DecisionContext
    questions: LayaQuestions

    def wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class LayaUsage(BaseModel):
    """How much of the context and questions Laya read."""

    #: Whether (or, read in windows, how often) the context was cut.
    truncated: int = 0
    #: The questions whose own budget cut the context.
    truncated_questions: list[str] = Field(default_factory=list)
    state_tokens_dropped: int = 0

    @property
    def cut(self) -> bool:
        return bool(self.truncated or self.truncated_questions)


class LayaRouting(BaseModel):
    """Which checkpoint the router sent the call to."""

    model: str | None = None


class LayaAnswer(BaseModel):
    """Laya's answer to one question; which fields it carries depends on the kind."""

    #: ``choice``: the label chosen.
    choice: str | None = None
    #: ``noul``: the probability of yes.
    noul: float | None = None
    #: ``score``: the expected level, 0 for the lowest.
    score: float | None = None
    #: ``choice``: by label; ``score``: by level index, as a map or a list.
    probabilities: dict[str, float] | list[float] = Field(default_factory=dict)

    def read(self, question: DecisionQuestion) -> DecisionAnswer:
        """This answer as igni's, in ``question``'s terms."""
        if question.kind is QuestionKind.NOUL:
            if self.noul is None:
                raise KeyError(f"{question.id}: Laya's answer has no noul probability")
            yes = self.noul
            return DecisionAnswer(
                kind=question.kind,
                answer="yes" if yes >= 0.5 else "no",
                confidence=max(yes, 1.0 - yes),
                probabilities={"yes": yes, "no": 1.0 - yes},
            )
        if question.kind is QuestionKind.SCORE:
            by_index = self._by_key()
            probabilities = {
                level: by_index.get(str(i), 0.0) for i, level in enumerate(question.levels or [])
            }
            top = max(probabilities, key=probabilities.__getitem__)
            return DecisionAnswer(
                kind=question.kind,
                answer=top,
                confidence=probabilities[top],
                probabilities=probabilities,
                score=self.score or 0.0,
            )
        probabilities = self._by_key()
        chosen = str(self.choice)
        return DecisionAnswer(
            kind=question.kind,
            answer=chosen,
            confidence=probabilities.get(chosen, 0.0),
            probabilities=probabilities,
        )

    def _by_key(self) -> dict[str, float]:
        if isinstance(self.probabilities, list):
            return {str(i): p for i, p in enumerate(self.probabilities)}
        return dict(self.probabilities)


class LayaResult(BaseModel):
    """Laya's result about one context."""

    answers: dict[str, LayaAnswer] = Field(default_factory=dict)
    usage: LayaUsage = Field(default_factory=LayaUsage)
    routing: LayaRouting = Field(default_factory=LayaRouting)

    def decision(
        self, questions: list[DecisionQuestion], *, allow_truncated: bool = False
    ) -> Decision:
        """Read back; a result Laya read only part of is refused unless ``allow_truncated``."""
        if not allow_truncated and self.usage.cut:
            raise DecisionTooLong(
                "the context and questions are longer than Laya reads "
                f"({self.usage.state_tokens_dropped} tokens of the context would be dropped); "
                "ask about less, or fewer questions at once"
            )
        return Decision(
            answers={q.id: self.answers[q.id].read(q) for q in questions},
            model=self.routing.model or "",
        )


__all__ = [
    "LayaAnswer",
    "LayaBatchItem",
    "LayaQuestion",
    "LayaQuestions",
    "LayaResult",
    "LayaRouting",
    "LayaUsage",
]
