"""Asks Laya whether a person's message thanks, praises or insults igni."""

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
    APPRECIATION_THRESHOLD,
    INSULT_THRESHOLD,
    MAX_MESSAGE_CHARS,
    MESSAGE_FIELD,
    SENTIMENT_QUESTION,
)
from igni_core.protocol.schemas.enums import SentimentKind

logger = logging.getLogger(__name__)


class SentimentJudge:
    """One choice question to Laya; an answer counts only when Laya is sure of it."""

    def __init__(self, laya: Laya = process_laya) -> None:
        self._laya = laya

    @staticmethod
    def worth_asking(text: str) -> bool:
        """Whether ``text``, stripped, is not empty and short enough to ask about."""
        return 0 < len(text.strip()) <= MAX_MESSAGE_CHARS

    async def judge(self, text: str) -> SentimentKind | None:
        """The sentiment Laya is sure ``text`` carries; None for anything else or no answer."""
        if not self.worth_asking(text):
            return None
        request = DecisionRequest(
            contexts=[DecisionContext({MESSAGE_FIELD: text.strip()})],
            questions=[SENTIMENT_QUESTION],
        )
        try:
            async with asyncio.timeout(ANSWER_TIMEOUT_S):
                decisions = await self._laya.decide(request)
        except (DecisionsUnavailable, DecisionTooLong, TimeoutError) as exc:
            logger.debug("sentiment: Laya gave no answer (%s: %s)", type(exc).__name__, exc)
            return None
        probabilities = decisions[0].answers[SENTIMENT_QUESTION.id].probabilities
        gratitude = probabilities.get(SentimentKind.GRATITUDE, 0.0)
        compliment = probabilities.get(SentimentKind.COMPLIMENT, 0.0)
        if probabilities.get(SentimentKind.INSULT, 0.0) >= INSULT_THRESHOLD:
            return SentimentKind.INSULT
        if gratitude + compliment >= APPRECIATION_THRESHOLD:
            return SentimentKind.GRATITUDE if gratitude >= compliment else SentimentKind.COMPLIMENT
        return None


__all__ = ["SentimentJudge"]
