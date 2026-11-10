"""Conversation-context management RPCs.

Thin coordinator over :class:`Session` + :class:`PendingMessageStore`
that fronts the RPCs the FE calls on the status / compaction /
history-truncation surface. Wire schemas
(:class:`TruncateHistoryResult`, :class:`PendingMessage`) live in
the sibling :mod:`schemas_context` module.

* :meth:`ContextController.get_status` — status-bar snapshot.
* :meth:`ContextController.count_context_tokens` — the conversation's
  size: the server's own count at the last answer, plus the tail after it.
* :meth:`ContextController.context_breakdown` — what that count is
  made of, for the ``/ctx`` page.
* :meth:`ContextController.compact_if_needed` — compaction on
  threshold cross.
* :meth:`ContextController.extract_learnings` — fire-and-forget
  learning-pipeline push (delegates to :meth:`Session.extract_learnings`).
* :meth:`ContextController.truncate_history` — drop a run and
  every later run from the Agno session.
* :meth:`ContextController.get_pending_messages` — surface
  pre-persisted user messages that never completed a run.
* :meth:`ContextController.get_interrupted_runs` — durable
  interrupted-run records (cancel + error stamped). Drives the
  FE's banner UI for show / retry / discard / edit-prompt.
* :meth:`ContextController.discard_interrupted_run` — hard-delete
  one interrupted record; FE "Discard" action.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from igni_core.backend.schemas_context import (
    PENDING_STALENESS_SECONDS,
    DiscardInterruptedRunResult,
    InterruptedRun,
    PendingMessage,
    TruncateHistoryResult,
)
from igni_core.core.config.group_policy.meta import PackMetaStore
from igni_core.core.config.group_policy.pack import PackTheme
from igni_core.core.config.models import ModelRegistry
from igni_core.core.session.persistence.run_history import RunHistory
from igni_core.core.session.schemas import ContextBreakdown
from igni_core.core.session.token_estimate import TokenEstimate
from igni_core.protocol import messages as msg

if TYPE_CHECKING:
    from igni_core.core.config.settings import Settings
    from igni_core.core.session import Session
    from igni_core.core.session.pending_messages import PendingMessageStore

logger = logging.getLogger(__name__)


class ContextController:
    """Context / status / compaction / learning / history-truncation
    controller for a single session."""

    def __init__(
        self,
        session: Session,
        settings: Settings,
        pending_store: PendingMessageStore,
        models: ModelRegistry,
        pack_meta: PackMetaStore,
    ) -> None:
        self._session = session
        self._settings = settings
        self._pending_store = pending_store
        self._models = models
        self._pack_meta = pack_meta

    def get_status(self) -> msg.StatusUpdate:
        """Status-bar snapshot.

        Everything here is a latched counter or a cheap attribute read
        except the group theme, which comes off the cached pack's
        metadata file — a couple of hundred bytes the OS has in cache,
        read on the same path :class:`PanelsController` already reads it
        on. Carried on the status push rather than its own message
        because the FE already re-renders on every one of these, and a
        second channel for six optional strings would be its own thing
        to keep in sync.

        The theme crosses the wire as the pass-through mapping
        :attr:`StatusUpdate.theme` declares; an empty one is no theme.
        """
        theme = self._group_theme()
        return msg.StatusUpdate(
            model=self._settings.models.default,
            model_configured=self._models.has_usable_model(),
            cloud_connected=self._session.cloud_connected,
            cloud_org=self._session.cloud_org_name or "",
            cloud_email=self._session.cloud_email or "",
            context_tokens=self._session.context_budget.last_input_tokens,
            max_context=self._settings.models.max_context_window,
            permission_mode=self._session.permission_mode_value,
            theme=(theme.model_dump() or None) if theme is not None else None,
        )

    def _group_theme(self) -> PackTheme | None:
        """Brand overrides from the cached group pack, if any.

        A missing or unreadable meta file reads as ``None`` already
        (:meth:`PackMetaStore.load`), so a status poll never fails on a
        branding lookup.
        """
        meta = self._pack_meta.load()
        return meta.theme if meta is not None else None

    async def context_breakdown(self) -> ContextBreakdown:
        """What the context is made of, for the ``/ctx`` page.

        ``count_context_tokens`` answers how big it is; this answers
        what it is made of — the conversation ``/compact`` can clear
        versus the floor (system prompt, tool schemas, project rules,
        memories, injected summary) that is rebaked into every prompt
        and cannot be compacted away.
        """
        return await self._session.context_breakdown()

    async def count_context_tokens(self) -> int:
        """The conversation's size: the server's count at the last answer, plus the tail after it."""
        try:
            agno_session = await self._session.main_team.aget_session(
                session_id=self._session.session_id,
                user_id=self._session.user_id,
            )
        except SQLAlchemyError:
            logger.warning("count_context_tokens: session read failed; reporting 0", exc_info=True)
            return 0
        if agno_session is None:
            return 0
        n = TokenEstimate.of(agno_session.get_messages())
        # Latch only when we actually measured something.
        if n > 0:
            self._session.context_budget.latch(n)
        return n

    async def compact_if_needed(self, ctx_tokens: int, max_ctx: int) -> msg.SessionCleared | None:
        """Compact session if approaching context limit."""
        compacted = await self._session.compact_if_needed(ctx_tokens, max_ctx)
        if not compacted:
            return None
        summary = ""
        try:
            agno_session = await self._session.main_team.aget_session(
                session_id=self._session.session_id,
                user_id=self._session.user_id,
            )
        except SQLAlchemyError:
            # The compaction itself succeeded; only the summary echo is lost.
            logger.warning("compact_if_needed: summary read failed", exc_info=True)
            agno_session = None
        if agno_session and agno_session.summary and agno_session.summary.summary:
            summary = agno_session.summary.summary
        return msg.SessionCleared(
            new_session_id=self._session.session_id,
            summary=summary,
        )

    async def extract_learnings(self, user_msg: str, assistant_msg: str) -> None:
        """Push a completed turn into the learning pipeline.

        Thin forward to :meth:`Session.extract_learnings` — the
        None-guard on the learning machine and the background-task
        launch both live on :class:`Session`.
        """
        await self._session.extract_learnings(user_msg, assistant_msg)

    async def truncate_history(self, session_id: str, run_id: str) -> TruncateHistoryResult:
        """Drop the run with ``run_id`` and every later run from the
        session."""
        agent = self._session.main_team
        agno_session = await agent.aget_session(
            session_id=session_id,
            user_id=self._session.user_id,
        )
        if agno_session is None:
            return TruncateHistoryResult(removed=0, error="session not found")
        runs = list(getattr(agno_session, "runs", None) or [])
        cut_idx: int | None = None
        for i, r in enumerate(runs):
            if getattr(r, "parent_run_id", None):
                continue
            if str(getattr(r, "run_id", "") or "") == run_id:
                cut_idx = i
                break
        if cut_idx is None:
            return TruncateHistoryResult(removed=0, error=f"run_id {run_id!r} not in session")
        removed = len(runs) - cut_idx
        agno_session.runs = runs[:cut_idx]
        try:
            # The cut runs are deleted from their own table (``run_history``).
            await RunHistory.save_for(agent, agno_session)
        except Exception as exc:
            logger.exception("truncate_history: save failed")
            return TruncateHistoryResult(removed=0, error=str(exc))
        # The latched context-token count was computed against the
        # pre-truncate session; invalidate it so the next status
        # read recomputes from the new (shorter) history.
        self._session.context_budget.latch(0)
        return TruncateHistoryResult(removed=removed)

    async def get_pending_messages(self, session_id: str) -> list[PendingMessage]:
        """Pending user messages that never completed a run.

        Oldest-first order. Only rows older than the staleness
        threshold are surfaced — a fresh row is almost always
        "Agno is still finishing its post-stream tail" and not a
        real interruption.
        """
        try:
            rows = await self._pending_store.alist_pending(session_id)
        except SQLAlchemyError:
            logger.warning("get_pending_messages: store read failed", exc_info=True)
            return []
        cutoff = int(time.time()) - PENDING_STALENESS_SECONDS
        rows = [r for r in rows if r.received_at <= cutoff]
        return [PendingMessage.from_pending_row(r) for r in rows]

    async def get_interrupted_runs(self, session_id: str) -> list[InterruptedRun]:
        """Durable interrupted-run records for the session.

        Distinct from :meth:`get_pending_messages` in three ways:

        1. Returns ONLY rows explicitly stamped by the cancel +
           error paths in :class:`RunController`. A row that's
           ``pending`` but never stamped (very old data, or the
           stamp write was lost on crash) is invisible here.
        2. No staleness cutoff — an interrupted row that just
           landed is the most useful one to surface (the user just
           hit Esc and is staring at the spinner).
        3. Carries ``reason`` + ``last_error`` so the FE can
           render the right banner label ("Run cancelled" vs
           "Run stopped — model timeout") and surface the error
           text on hover.

        Returns wire models via
        :meth:`InterruptedRun.from_interrupted_row` so the
        controller stays out of the field-mapping business.
        """
        try:
            rows = await self._pending_store.alist_interrupted(session_id)
        except SQLAlchemyError:
            logger.warning("get_interrupted_runs: store read failed", exc_info=True)
            return []
        return [InterruptedRun.from_interrupted_row(r) for r in rows]

    async def discard_interrupted_run(
        self, session_id: str, message_id: str
    ) -> DiscardInterruptedRunResult:
        """Hard-delete one interrupted run — the FE's "Discard" action.

        Idempotent: deleting a row that no longer exists returns
        ``ok=True`` so the FE can stop polling / re-trying. The
        row is matched by ``session_id`` and ``message_id`` (the
        wire-level id we exposed via :meth:`get_interrupted_runs`),
        and only an interrupted run is deleted. A storage failure is
        ``ok=False`` with the error.

        Does NOT touch other interrupted rows for the session —
        only the one the user picked. The "discard all" semantics
        are reserved for the ``user_message(force=true)`` retry
        path (see :meth:`PendingMessageStore.discard_all_for_session`).
        """
        if not message_id:
            return DiscardInterruptedRunResult(ok=False, error="message_id is required")
        try:
            await self._pending_store.adiscard_interrupted(session_id, message_id)
        except SQLAlchemyError as exc:
            logger.warning("discard_interrupted_run failed: %s", exc)
            return DiscardInterruptedRunResult(ok=False, error=str(exc))
        return DiscardInterruptedRunResult(ok=True)
