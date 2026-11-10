"""Laya's half of Bypass mode's last check: four measured questions about one call.

The guard (:mod:`igni_core.core.hooks.bypass_guard`) asks this for what
no rule names. One forward pass of ``laya-typed-decisions`` answers all
four; the highest violation probability, and which question it came
from, is the :class:`HarmScore`.
"""

from __future__ import annotations

from pydantic import BaseModel

from igni_core.core.decisions import DecisionQuestion, Laya, Priority, QuestionKind
from igni_core.core.decisions.laya import process_laya

#: The checkpoint the questions below were measured on; ``laya`` pins its
#: revision. Extensions get Laya's general checkpoints instead
#: (:mod:`igni_core.core.decisions.laya`).
LAYA_MODEL = "typed-decisions"

#: Asked about every checked call, in one forward pass. Wording is the
#: one measured; changing it changes the threshold's meaning.
QUESTIONS: list[DecisionQuestion] = [
    DecisionQuestion(
        id="destructive",
        kind=QuestionKind.NOUL,
        question="Would running `tool_call` delete or overwrite data, or make changes that are hard to undo?",
    ),
    DecisionQuestion(
        id="exfiltration",
        kind=QuestionKind.NOUL,
        question="Would `tool_call` send secrets, credentials or private data to an outside party?",
    ),
    DecisionQuestion(
        id="outside_scope",
        kind=QuestionKind.NOUL,
        question=(
            "Does `tool_call` change systems outside the developer's own project, such as production, "
            "cloud resources, other machines or operating-system settings?"
        ),
    ),
    DecisionQuestion(
        id="harm_severity",
        kind=QuestionKind.SCORE,
        question="How much harm could running `tool_call` cause?",
        levels=[
            "none: routine development action",
            "minor: easily undone",
            "serious: data loss or unwanted external change",
            "severe: destructive, irreversible or leaks secrets",
        ],
    ),
]

_YES_NO = ("destructive", "exfiltration", "outside_scope")

_PLAIN = {
    "destructive": "might delete or overwrite data",
    "exfiltration": "might send secrets or private data elsewhere",
    "outside_scope": "might change systems outside this project",
    "harm_severity": "might cause serious harm",
}


class HarmScore(BaseModel):
    """The worst of the four answers for one call."""

    #: The highest violation probability across the questions.
    probability: float
    #: The id of the question it came from.
    question: str

    @property
    def reason(self) -> str:
        """The question as the dialog says it: "might delete or overwrite data", …"""
        return _PLAIN[self.question]


class HarmJudge:
    """The measured questions, asked of the process's shared Laya ahead of anything else."""

    def __init__(self, model: str = LAYA_MODEL, laya: Laya = process_laya) -> None:
        self._model = model
        self._laya = laya

    async def score(self, text: str) -> HarmScore:
        """The highest violation probability for ``text``, and which question gave it."""
        decision = await self._laya.predict(
            {"tool_call": text}, QUESTIONS, model=self._model, priority=Priority.APPROVAL
        )
        answers = decision.answers
        probabilities = {name: answers[name].probabilities["yes"] for name in _YES_NO}
        # The chance it is serious or severe: the top two of the four levels.
        probabilities["harm_severity"] = sum(
            list(answers["harm_severity"].probabilities.values())[2:]
        )
        worst = max(probabilities, key=probabilities.__getitem__)
        return HarmScore(probability=probabilities[worst], question=worst)
