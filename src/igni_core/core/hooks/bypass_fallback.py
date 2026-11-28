"""Bypass's last check when Laya cannot judge: the rules alone decide, and the process says so once."""

from __future__ import annotations

import logging

from igni_core.core.decisions import DecisionsUnavailable

logger = logging.getLogger(__name__)


class RulesOnlyFallback:
    """Warns once per process that the safety model is out, why, and how to fix it."""

    def __init__(self) -> None:
        self._warned = False

    def note(self, exc: Exception) -> None:
        """Record that Laya could not judge a call; the first time, say why and what fixes it."""
        if self._warned:
            return
        self._warned = True
        logger.warning(self.notice(exc))

    @staticmethod
    def notice(exc: Exception) -> str:
        """The warning: the safety model is not available, why, and how to turn it back on."""
        why = str(exc) if isinstance(exc, DecisionsUnavailable) and str(exc) else type(exc).__name__
        return (
            f"Bypass safety check: the safety model is not available ({why}), so only its rules "
            "decide what to ask about. Install igni-core[decisions] to turn it back on."
        )


#: The process's one fallback, shared by every session's guard, so the warning comes once.
process_fallback = RulesOnlyFallback()


__all__ = ["RulesOnlyFallback", "process_fallback"]
