"""Home for :class:`InterruptedTurnRecall`."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from agno.run.base import HISTORY_SKIP_STATUSES
from sqlalchemy.exc import SQLAlchemyError

from igni_core.backend.server_interrupted_run import (
    AGNO_SESSION_READ_ERRORS,
    InterruptedRunEvidence,
)

if TYPE_CHECKING:
    from igni_core.backend.schemas_lifecycle import InterruptedRunSummary
    from igni_core.core.session import Session
    from igni_core.core.session.pending_messages import InterruptedMessage, PendingMessageStore

logger = logging.getLogger(__name__)


class InterruptedTurnRecall:
    """Recaps the stamped turns of the current chat that Agno's history leaves out."""

    def __init__(self, session: Session, pending_store: PendingMessageStore) -> None:
        self._session = session
        self._store = pending_store

    async def take(self) -> InterruptedRunSummary | None:
        """The recap of the current chat's interrupted turns the history does not show."""
        rows = await self._stamped_rows()
        if not rows:
            return None
        runs = await self._shown_runs()
        unseen: list[InterruptedMessage] = []
        for row in rows:
            if any(self._is_turn_of(run, row) for run in runs):
                await self._store.adiscard(row.message_id)
            else:
                unseen.append(row)
        if not unseen:
            return None
        return InterruptedRunEvidence.from_stamped(unseen).summary()

    async def _stamped_rows(self) -> list[InterruptedMessage]:
        try:
            return await self._store.alist_interrupted(self._session.session_id)
        except SQLAlchemyError as exc:
            logger.warning("interrupted-turn recall: reading stamped rows failed: %s", exc)
            return []

    async def _shown_runs(self) -> list[Any]:
        """The chat's runs Agno puts in the history it shows the model."""
        try:
            agno_session = await self._session.main_team.aget_session(
                session_id=self._session.session_id,
            )
        except AGNO_SESSION_READ_ERRORS as exc:
            logger.warning("interrupted-turn recall: Agno session unavailable: %s", exc)
            return []
        runs = getattr(agno_session, "runs", None) or []
        return [run for run in runs if getattr(run, "status", None) not in HISTORY_SKIP_STATUSES]

    @staticmethod
    def _is_turn_of(run: Any, row: InterruptedMessage) -> bool:
        """Whether ``run`` answered ``row``'s message: started with it, no earlier than it."""
        run_input = getattr(run, "input", None)
        if run_input is None or getattr(run, "created_at", 0) < row.received_at:
            return False
        return row.text in run_input.input_content_string()
