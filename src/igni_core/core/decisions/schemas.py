"""Typed decisions, as igni asks them of Laya and answers them.

A decision is a few named questions about a context — named text fields
the questions refer to as `` `field` `` — each one of Laya's three
kinds:

* ``choice``: one of up to :attr:`DecisionLimits.OPTIONS` labelled options;
* ``score``: one level of an ordered scale, lowest first;
* ``noul``: yes or no (Laya's name for it).

Every answer carries the probabilities it came from, so the asker
decides how sure is sure enough. The limits are the host's, not
Laya's: they keep one extension from holding the model for long, and
they keep a question inside what the model can read at all.
"""

from __future__ import annotations

import re
from enum import IntEnum, StrEnum

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator

#: A context field's name, as a question quotes it: `` `diff` ``.
FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class DecisionLimits:
    """How much one call may ask."""

    #: Named fields in one context.
    CONTEXT_FIELDS = 8
    #: Characters across one context's fields. Laya reads 512 tokens
    #: (English) or 1,024 (the others) including the questions; this
    #: refuses the obviously too long before the model is asked, and
    #: :class:`DecisionTooLong` refuses what the model would truncate.
    CONTEXT_CHARS = 6000
    #: Questions in one call.
    QUESTIONS = 16
    #: Characters in one question.
    QUESTION_CHARS = 500
    #: Options of a ``choice`` question.
    OPTIONS = 20
    #: Levels of a ``score`` question.
    LEVELS = 10
    #: Characters in an option's or a level's label.
    LABEL_CHARS = 64
    #: Characters in an option's description.
    DESCRIPTION_CHARS = 200
    #: Contexts in one batch.
    BATCH = 16


class QuestionKind(StrEnum):
    CHOICE = "choice"
    SCORE = "score"
    NOUL = "noul"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DecisionQuestion(Strict):
    """One question, as an extension writes it."""

    id: str = Field(pattern=FIELD_NAME.pattern, max_length=64)
    kind: QuestionKind
    question: str = Field(min_length=1, max_length=DecisionLimits.QUESTION_CHARS)
    #: ``choice`` only: label → what it means ("" when the label says it).
    options: dict[str, str] | None = None
    #: ``score`` only: the levels, lowest first.
    levels: list[str] | None = None

    @model_validator(mode="after")
    def _fits_its_kind(self) -> DecisionQuestion:
        if self.kind is QuestionKind.CHOICE:
            if self.levels is not None:
                raise ValueError(f"{self.id}: a choice question has options, not levels")
            options = self.options or {}
            if not 2 <= len(options) <= DecisionLimits.OPTIONS:
                raise ValueError(
                    f"{self.id}: a choice question has 2 to {DecisionLimits.OPTIONS} options"
                )
            for label, description in options.items():
                self._label(label)
                if len(description) > DecisionLimits.DESCRIPTION_CHARS:
                    raise ValueError(
                        f"{self.id}: option {label!r}'s description is over "
                        f"{DecisionLimits.DESCRIPTION_CHARS} characters"
                    )
        elif self.kind is QuestionKind.SCORE:
            if self.options is not None:
                raise ValueError(f"{self.id}: a score question has levels, not options")
            levels = self.levels or []
            if not 2 <= len(levels) <= DecisionLimits.LEVELS:
                raise ValueError(
                    f"{self.id}: a score question has 2 to {DecisionLimits.LEVELS} levels"
                )
            if len(set(levels)) != len(levels):
                raise ValueError(f"{self.id}: every level is named differently")
            for label in levels:
                self._label(label)
        elif self.options is not None or self.levels is not None:
            raise ValueError(f"{self.id}: a noul (yes/no) question has no options or levels")
        return self

    def _label(self, label: str) -> None:
        if not label.strip() or len(label) > DecisionLimits.LABEL_CHARS:
            raise ValueError(
                f"{self.id}: a label is 1 to {DecisionLimits.LABEL_CHARS} characters, not {label!r}"
            )


class DecisionContext(RootModel[dict[str, str]]):
    """One context: named text fields the questions quote as `` `field` ``."""

    @model_validator(mode="after")
    def _fits(self) -> DecisionContext:
        if not 1 <= len(self.root) <= DecisionLimits.CONTEXT_FIELDS:
            raise ValueError(f"a context has 1 to {DecisionLimits.CONTEXT_FIELDS} named fields")
        for name in self.root:
            if not FIELD_NAME.match(name):
                raise ValueError(f"a context field is a lowercase name, not {name!r}")
        if sum(len(text) for text in self.root.values()) > DecisionLimits.CONTEXT_CHARS:
            raise ValueError(
                f"a context is at most {DecisionLimits.CONTEXT_CHARS} characters; "
                "ask about less of it"
            )
        return self


class DecisionRequest(Strict):
    """Contexts and the questions asked of each."""

    contexts: list[DecisionContext] = Field(min_length=1, max_length=DecisionLimits.BATCH)
    questions: list[DecisionQuestion] = Field(min_length=1, max_length=DecisionLimits.QUESTIONS)

    @field_validator("questions")
    @classmethod
    def _distinct(cls, questions: list[DecisionQuestion]) -> list[DecisionQuestion]:
        ids = [q.id for q in questions]
        if len(set(ids)) != len(ids):
            raise ValueError("every question has its own id")
        return questions


class DecisionAnswer(BaseModel):
    """One question's answer."""

    kind: QuestionKind
    #: The option chosen, the level reached, or ``yes`` / ``no``.
    answer: str
    #: The probability of that answer.
    confidence: float
    #: Every option's, level's, or ``yes`` and ``no``'s probability.
    probabilities: dict[str, float]
    #: ``score`` only: the expected level, 0 for the lowest.
    score: float | None = None


class Decision(BaseModel):
    """Every question's answer about one context."""

    answers: dict[str, DecisionAnswer]
    #: The Laya checkpoint that answered.
    model: str


class Priority(IntEnum):
    """Who waits for whom at the model. Lower goes first."""

    APPROVAL = 0
    EXTENSION = 1


class DecisionsUnavailable(Exception):
    """The decisions service could not be reached, or Laya could not load or run; nothing was decided."""


class DecisionTooLong(Exception):
    """The context and questions do not fit what Laya reads."""


__all__ = [
    "FIELD_NAME",
    "Decision",
    "DecisionAnswer",
    "DecisionContext",
    "DecisionLimits",
    "DecisionQuestion",
    "DecisionRequest",
    "DecisionTooLong",
    "DecisionsUnavailable",
    "Priority",
    "QuestionKind",
    "Strict",
]
