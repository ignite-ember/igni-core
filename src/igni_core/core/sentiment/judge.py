"""Asks Laya whether a person's message is pleased or displeased with igni."""

from __future__ import annotations

import asyncio
import logging

from igni_core.core.decisions.laya import Laya, process_laya
from igni_core.core.decisions.schemas import (
    DecisionContext,
    DecisionRequest,
    DecisionsUnavailable,
    DecisionTooLong,
)
from igni_core.core.sentiment.constants import (
    ANSWER_TIMEOUT_S,
    GESTURE_QUESTION,
    INSULT_THRESHOLD,
    MAX_MESSAGE_CHARS,
    MESSAGE_FIELD,
    NEGATIVE_THRESHOLD,
    POSITIVE_THRESHOLD,
    SENTIMENT_QUESTION,
)
from igni_core.core.sentiment.schemas import SentimentVerdict
from igni_core.protocol.schemas.enums import SentimentKind

logger = logging.getLogger(__name__)


class SentimentJudge:
    """Two choice questions in one call to Laya; a verdict only when Laya is sure."""

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
            questions=[SENTIMENT_QUESTION, GESTURE_QUESTION],
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
        if displeased >= NEGATIVE_THRESHOLD or insult >= INSULT_THRESHOLD:
            return SentimentVerdict(
                sentiment=SentimentKind.NEGATIVE, confidence=max(displeased, insult)
            )
        pleased = gesture.get("gratitude", 0.0) + gesture.get("compliment", 0.0)
        if pleased >= POSITIVE_THRESHOLD:
            return SentimentVerdict(sentiment=SentimentKind.POSITIVE, confidence=pleased)
        return None


__all__ = ["SentimentJudge"]
