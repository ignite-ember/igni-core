"""Asks Laya whether a person's message is pleased or displeased with igni."""

from __future__ import annotations

import asyncio
import logging

from igni_core.core.decisions.laya import Laya, process_laya
from igni_core.core.decisions.schemas import (
    DecisionAnswer,
    DecisionContext,
    DecisionRequest,
    DecisionsUnavailable,
    DecisionTooLong,
)
from igni_core.core.sentiment.constants import (
    ANSWER_TIMEOUT_S,
    ASPECT_QUESTIONS,
    GESTURE_QUESTION,
    INSULT_THRESHOLD,
    INTENSITY_QUESTION,
    MAX_MESSAGE_CHARS,
    MESSAGE_FIELD,
    NEGATIVE_REASON_QUESTION,
    NEGATIVE_THRESHOLD,
    POSITIVE_REASON_QUESTION,
    POSITIVE_THRESHOLD,
    REDO_QUESTION,
    REPEAT_QUESTION,
    SENTIMENT_QUESTION,
    TASK_DONE_QUESTION,
)
from igni_core.core.sentiment.schemas import SentimentVerdict
from igni_core.protocol.schemas.enums import SentimentKind
from igni_core.protocol.schemas.sentiment.label_probability import LabelProbability
from igni_core.protocol.schemas.sentiment.sentiment_aspect import SentimentAspect
from igni_core.protocol.schemas.sentiment.sentiment_aspects import SentimentAspects

logger = logging.getLogger(__name__)


class SentimentJudge:
    """Every question in one call to Laya; a verdict only when Laya is sure, its aspects beside it."""

    def __init__(self, laya: Laya = process_laya) -> None:
        self._laya = laya

    @staticmethod
    def worth_asking(text: str) -> bool:
        """Whether ``text``, stripped, is not empty and short enough for Laya to read whole."""
        return 0 < len(text.strip()) <= MAX_MESSAGE_CHARS

    async def judge(self, text: str) -> SentimentVerdict | None:
        """Negative when displeasure or an insult is sure, else positive when thanks or praise is."""
        if not self.worth_asking(text):
            return None
        request = DecisionRequest(
            contexts=[DecisionContext({MESSAGE_FIELD: text.strip()})],
            questions=[SENTIMENT_QUESTION, GESTURE_QUESTION, *ASPECT_QUESTIONS],
        )
        try:
            async with asyncio.timeout(ANSWER_TIMEOUT_S):
                decisions = await self._laya.decide(request)
        except (DecisionsUnavailable, DecisionTooLong, TimeoutError) as exc:
            logger.debug("sentiment: Laya gave no answer (%s: %s)", type(exc).__name__, exc)
            return None
        answers = decisions[0].answers
        feeling = answers[SENTIMENT_QUESTION.id].probabilities
        gesture = answers[GESTURE_QUESTION.id].probabilities
        displeased = feeling.get("negative", 0.0)
        insult = gesture.get("insult", 0.0)
        pleased = gesture.get("gratitude", 0.0) + gesture.get("compliment", 0.0)
        if displeased >= NEGATIVE_THRESHOLD or insult >= INSULT_THRESHOLD:
            sentiment, confidence = SentimentKind.NEGATIVE, max(displeased, insult)
        elif pleased >= POSITIVE_THRESHOLD:
            sentiment, confidence = SentimentKind.POSITIVE, pleased
        else:
            return None
        aspects = SentimentAspects(
            feeling=_aspect(answers[SENTIMENT_QUESTION.id]),
            gesture=_aspect(answers[GESTURE_QUESTION.id]),
            negative_reason=_aspect(answers[NEGATIVE_REASON_QUESTION.id]),
            positive_reason=_aspect(answers[POSITIVE_REASON_QUESTION.id]),
            repeat=_aspect(answers[REPEAT_QUESTION.id]),
            redo_requested=_aspect(answers[REDO_QUESTION.id]),
            task_done=_aspect(answers[TASK_DONE_QUESTION.id]),
            intensity=_aspect(answers[INTENSITY_QUESTION.id]),
        )
        return SentimentVerdict(sentiment=sentiment, confidence=confidence, aspects=aspects)


def _aspect(answer: DecisionAnswer) -> SentimentAspect:
    """Laya's chosen label and every label's probability, as the push carries them."""
    return SentimentAspect(
        answer=answer.answer,
        probabilities=[
            LabelProbability(label=label, probability=probability)
            for label, probability in answer.probabilities.items()
        ],
    )


__all__ = ["SentimentJudge"]
