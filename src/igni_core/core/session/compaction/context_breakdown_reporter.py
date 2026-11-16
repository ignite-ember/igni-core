"""Context-breakdown reporter for the ``/ctx`` slash command.

Small collaborator that owns the token accounting used to
explain why ``/compact`` cannot shrink the context all the way
down. Extracted out of :class:`CompactionCoordinator` so the
coordinator stays focused on the compaction-lifecycle
concern.
"""

from __future__ import annotations

import logging
from typing import Any

from agno.models.message import Message as AgnoMessage

from igni_core.core.session.compaction.team_session import TeamSessionSource
from igni_core.core.session.schemas import ContextBreakdown
from igni_core.core.session.token_estimate import TokenEstimate

logger = logging.getLogger(__name__)


class ContextBreakdownReporter:
    """Splits Agno's assembled message list into ``runs`` +
    ``floor`` token counts for the ``/ctx`` slash command.

    Composed by :class:`CompactionCoordinator`. The floor-
    clamp invariant (``floor = max(0, total - runs)``) lives on
    :meth:`ContextBreakdown.from_totals` — this reporter just
    supplies the two totals.
    """

    def __init__(self, source: TeamSessionSource) -> None:
        self._source = source

    #: ``session_data`` key holding the floor measured just before a
    #: compaction cleared the runs, without the summary of that time.
    FLOOR_KEY = "igni_context_floor"

    async def report(self) -> ContextBreakdown:
        """Return per-component token counts for the current context.

        * ``runs``  — conversational turns (user / assistant / tool
          messages held under ``agno_session.runs``).
        * ``floor`` — everything else baked into every prompt: system
          instructions, tool schemas, project rules, active memories,
          injected session summary.
        * ``total`` — the full assembled-message figure
          (``total == runs + floor``).

        ``total`` is the server's own count at the last answer plus an
        estimate for what came after it; ``runs`` is estimated from the
        turns' length (nothing counts them apart from the floor). Right
        after a compaction there is no answer left to read the floor
        off, so it is the floor :meth:`remember_floor` kept, plus the
        summary that replaced the turns.
        Returns a zero-filled breakdown on any I/O failure so the
        ``/ctx`` card renders a well-formed empty state instead of
        erroring out.
        """
        try:
            agno_session = await self._source.load()
        except Exception:
            return ContextBreakdown.from_totals(total=0, runs=0)
        if agno_session is None:
            return ContextBreakdown.from_totals(total=0, runs=0)
        breakdown = self.measure(agno_session)
        if breakdown.total == 0 and not self._run_messages(agno_session):
            kept = (getattr(agno_session, "session_data", None) or {}).get(self.FLOOR_KEY)
            if isinstance(kept, int) and kept > 0:
                floor = kept + self._summary_tokens(agno_session)
                return ContextBreakdown.from_totals(total=floor, runs=0)
        return breakdown

    @classmethod
    def measure(cls, agno_session: Any) -> ContextBreakdown:
        """The breakdown of ``agno_session`` as its messages give it."""
        try:
            total = TokenEstimate.of(agno_session.get_messages())
        except Exception:
            total = 0
        runs_tokens = min(TokenEstimate.chars(cls._run_messages(agno_session)), total)
        return ContextBreakdown.from_totals(total=total, runs=runs_tokens)

    @classmethod
    def remember_floor(cls, agno_session: Any, previous_summary: str | None) -> None:
        """Keep the floor on ``agno_session`` before its runs are cleared.

        The floor is read off the last answer, which the compaction is
        about to delete; ``previous_summary`` (part of that floor) is
        taken out, because the new summary replaces it.
        """
        floor = cls.measure(agno_session).floor
        if floor <= 0:
            return
        if previous_summary:
            floor = max(0, floor - TokenEstimate.text(len(previous_summary)))
        data = dict(getattr(agno_session, "session_data", None) or {})
        data[cls.FLOOR_KEY] = floor
        agno_session.session_data = data

    @staticmethod
    def _summary_tokens(agno_session: Any) -> int:
        text = getattr(getattr(agno_session, "summary", None), "summary", None)
        if not text:
            return 0
        return TokenEstimate.text(len(text))

    @staticmethod
    def _run_messages(agno_session: Any) -> list[AgnoMessage]:
        run_messages: list[AgnoMessage] = []
        for run in getattr(agno_session, "runs", None) or []:
            msgs = getattr(run, "messages", None)
            if msgs:
                # Agno stores each run's system message with it; that is
                # the floor every prompt carries, not a turn.
                run_messages.extend(m for m in msgs if getattr(m, "role", None) != "system")
        return run_messages
