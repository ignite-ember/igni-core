"""Asks Laya whether a person's message thanks or praises igni."""

from __future__ import annotations

import asyncio
import logging

from igni_core.core.appreciation.constants import (
    ANSWER_TIMEOUT_S,
    APPRECIATION_QUESTION,
    CONFIDENCE_THRESHOLD,
    MAX_MESSAGE_CHARS,
    MESSAGE_FIELD,
)
from igni_core.core.decisions.laya import Laya, process_laya
from igni_core.core.decisions.schemas import (
    DecisionContext,
    DecisionRequest,
    DecisionsUnavailable,
    DecisionTooLong,
)
from igni_core.protocol.schemas.enums import AppreciationKind

logger = logging.getLogger(__name__)


class AppreciationJudge:
    """One choice question to Laya; the answer counts only when Laya is sure of it."""

    def __init__(self, laya: Laya = process_laya) -> None:
        self._laya = laya

    @staticmethod
    def worth_asking(text: str) -> bool:
        """Whether ``text``, stripped, is not empty and short enough to be thanks."""
        return 0 < len(text.strip()) <= MAX_MESSAGE_CHARS

    async def judge(self, text: str) -> AppreciationKind | None:
        """The appreciation Laya is sure ``text`` carries; None for anything else or no answer."""
        if not self.worth_asking(text):
            return None
        request = DecisionRequest(
            contexts=[DecisionContext({MESSAGE_FIELD: text.strip()})],
            questions=[APPRECIATION_QUESTION],
        )
        try:
            async with asyncio.timeout(ANSWER_TIMEOUT_S):
                decisions = await self._laya.decide(request)
        except (DecisionsUnavailable, DecisionTooLong, TimeoutError) as exc:
            logger.debug("appreciation: Laya gave no answer (%s: %s)", type(exc).__name__, exc)
            return None
        answer = decisions[0].answers[APPRECIATION_QUESTION.id]
        if answer.confidence < CONFIDENCE_THRESHOLD:
            return None
        if answer.answer == AppreciationKind.GRATITUDE:
            return AppreciationKind.GRATITUDE
        if answer.answer == AppreciationKind.COMPLIMENT:
            return AppreciationKind.COMPLIMENT
        return None


__all__ = ["AppreciationJudge"]
