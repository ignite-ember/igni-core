"""Home for :class:`InterruptedRunSummaryBuilder`.

Detects, at startup, whether the previous launch stopped mid-run and
turns what it finds into an :class:`InterruptedRunSummary`.

Two kinds of interruption leave two kinds of evidence:

1. **Stamped** — the cancel and error paths in :class:`RunController`
   stamp the pending row with ``interrupted_at`` (and the error, if
   any). ``alist_interrupted`` returns these rows; they are the
   authoritative record and short-circuit everything else.
2. **Unstamped** — a hard crash (kill, power loss, OOM) runs no
   cancel or error path, so nothing is stamped. What remains is a
   still-``pending`` row and, when Agno got far enough to write the
   session, a last run left at ``RunStatus.running``. This is not a
   compatibility path: it is the only trace a hard crash leaves.

:class:`InterruptedRunEvidence` holds whichever evidence was found
and renders the recap from it, so the prose is assembled in one place.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agno.run.base import RunStatus
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from igni_core.backend.schemas_lifecycle import (
    AgnoRunSnapshot,
    InterruptedRunSummary,
)
from igni_core.core.session.pending_messages import (
    InterruptedMessage,
    PendingMessage,
)

if TYPE_CHECKING:
    from igni_core.core.session import Session
    from igni_core.core.session.pending_messages import PendingMessageStore

logger = logging.getLogger(__name__)

# What reading Agno's session can raise. The bundled Agno re-raises every
# storage error rather than returning None: ValueError when its db is not
# initialised (and JSONDecodeError / pydantic ValidationError, both
# ValueError subclasses, from a half-written row), KeyError / TypeError
# when a row deserialises into the wrong shape, SQLAlchemyError for a
# locked or corrupt database file. A hard crash is exactly what leaves
# such a row, so none of these may block startup.
_AGNO_SESSION_READ_ERRORS: tuple[type[Exception], ...] = (
    ValueError,
    KeyError,
    TypeError,
    SQLAlchemyError,
)


class InterruptedRunEvidence(BaseModel):
    """What startup found about an interrupted previous run.

    ``asked`` are the user messages the interrupted run was answering
    (stamped rows when there are any, otherwise the unstamped pending
    rows). ``error`` is the recorded failure of an ``errored`` stamped
    row. ``snapshot`` is Agno's still-running last run, probed only on
    the unstamped path.
    """

    model_config = ConfigDict(frozen=True)

    asked: list[PendingMessage | InterruptedMessage] = Field(default_factory=list)
    error: str | None = None
    snapshot: AgnoRunSnapshot | None = None
    stamped: bool = False

    @classmethod
    def from_stamped(cls, rows: list[InterruptedMessage]) -> InterruptedRunEvidence:
        error = next(
            (r.last_error for r in rows if r.interrupted_reason == "errored" and r.last_error),
            None,
        )
        return cls(asked=list(rows), error=error, stamped=True)

    @property
    def found(self) -> bool:
        return bool(self.asked) or self.snapshot is not None

    def recap(self) -> str:
        parts = ["Previous run was interrupted before completion."]
        if len(self.asked) == 1:
            parts.append(f"The user had asked: {self.asked[0].text!r}.")
        elif self.asked:
            questions = "; ".join(m.text for m in self.asked)
            parts.append(f"The user had pending question(s): {questions!r}.")
        if self.error:
            parts.append(f"Reason: {self.error!r}.")
        if self.snapshot is not None:
            if self.snapshot.tool_names:
                parts.append(f"Tool calls completed: {', '.join(self.snapshot.tool_names)}.")
            if self.snapshot.content_preview.strip():
                parts.append(f"Partial response so far: {self.snapshot.content_preview!r}.")
        return " ".join(parts)

    def summary(self) -> InterruptedRunSummary:
        # The closing instruction is chosen at delivery, not here — see
        # ``InterruptedRunSummary.render``. This runs at startup, where the
        # only honest thing to say is what happened.
        return InterruptedRunSummary(
            recap=self.recap(),
            pending_ids_to_drop=[m.message_id for m in self.asked],
        )


class InterruptedRunSummaryBuilder:
    """Assemble an :class:`InterruptedRunSummary` from the pending-message
    store and Agno's session.

    One instance per :class:`LifecycleController`. Owns the store
    queries and the Agno probe; the prose lives on
    :class:`InterruptedRunEvidence`.
    """

    def __init__(
        self,
        session: Session,
        pending_store: PendingMessageStore | None,
    ) -> None:
        self._session = session
        self._pending_store = pending_store

    async def build(self) -> InterruptedRunSummary | None:
        """Return a summary if the previous run was interrupted, else
        ``None`` (clean shutdown).

        A storage failure while probing is logged and treated as "no
        evidence" — a broken detection must not block startup.
        """
        evidence = await self._collect()
        if not evidence.found:
            return None
        logger.info(
            "detected interrupted previous run (stamped=%s, rows=%d, agno_run=%s); "
            "summary will be injected on next user message",
            evidence.stamped,
            len(evidence.asked),
            evidence.snapshot.run_id if evidence.snapshot is not None else None,
        )
        return evidence.summary()

    async def _collect(self) -> InterruptedRunEvidence:
        stamped = await self._interrupted_rows()
        if stamped:
            return InterruptedRunEvidence.from_stamped(stamped)
        return InterruptedRunEvidence(
            asked=list(await self._pending_rows()),
            snapshot=await self._running_agno_run(),
        )

    async def _running_agno_run(self) -> AgnoRunSnapshot | None:
        """Agno's last run, if it was left at ``RunStatus.running``."""
        try:
            agno_session = await self._session.main_team.aget_session(
                session_id=self._session.session_id,
            )
        except _AGNO_SESSION_READ_ERRORS as exc:
            logger.warning("interrupted-run: Agno session unavailable: %s", exc)
            return None
        runs = getattr(agno_session, "runs", None) or []
        if not runs or getattr(runs[-1], "status", None) != RunStatus.running:
            return None
        return AgnoRunSnapshot.from_agno_run(runs[-1])

    async def _interrupted_rows(self) -> list[InterruptedMessage]:
        if self._pending_store is None:
            return []
        try:
            return await self._pending_store.alist_interrupted(self._session.session_id)
        except SQLAlchemyError as exc:
            logger.warning("interrupted-run: reading stamped rows failed: %s", exc)
            return []

    async def _pending_rows(self) -> list[PendingMessage]:
        if self._pending_store is None:
            return []
        try:
            return await self._pending_store.alist_pending(self._session.session_id)
        except SQLAlchemyError as exc:
            logger.warning("interrupted-run: reading pending rows failed: %s", exc)
            return []
