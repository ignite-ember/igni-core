"""Compaction coordinator — orchestrates the auto + manual paths.

Extracted from :mod:`igni_core.core.session.core` — the code
that summarises the conversation and drops old runs to free
context. Auto-compaction fires at 80% context usage;
:meth:`CompactionCoordinator.force_compact` triggers the manual
``/compact`` path; :meth:`CompactionCoordinator.context_breakdown`
is delegated to :class:`ContextBreakdownReporter`.

The coordinator composes two collaborators:

* :class:`FallbackSummariser` — runs a free-text summariser
  when Agno's structured :class:`SessionSummaryManager` returns
  empty (MiniMax-M2.7 workaround; see the summariser's own
  module docstring for the full story).
* :class:`ContextBreakdownReporter` — owns the ``/ctx`` token
  accounting.

Both public entry points return a
:class:`~igni_core.core.session.schemas.CompactResult`
envelope so callers stop tuple-unpacking or ``str | None``-
checking at every call site.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from agno.session.summary import SessionSummary, SessionSummaryManager

from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.session.compaction.context_breakdown_reporter import (
    ContextBreakdownReporter,
)
from igni_core.core.session.compaction.fallback_summariser import FallbackSummariser
from igni_core.core.session.compaction.team_session import TeamSessionSource
from igni_core.core.session.persistence.run_history import RunHistory
from igni_core.core.session.schemas import (
    CompactResult,
    ContextBreakdown,
    PostCompactHookPayload,
    PreCompactHookPayload,
)

logger = logging.getLogger(__name__)


class CompactionCoordinator:
    """Owns compaction (auto + manual) plus the context-breakdown
    reporter.

    Handed a :class:`TeamSessionSource` (the live ``main_team``,
    reassigned by :meth:`compact`, plugin-reload and MCP-rebuild, is
    read through it at call time), the hook executor, and
    ``rebuild_team`` — rebuilding from scratch is the only reliable
    way to clear Agno's in-memory message cache.
    """

    _COMPACT_THRESHOLD = 0.8

    def __init__(
        self,
        source: TeamSessionSource,
        hook_executor_ref: Callable[[], HookExecutor],
        rebuild_team: Callable[[], None],
    ) -> None:
        self._source = source
        # A callable: a hook reload builds a new executor.
        self._hook_executor_ref = hook_executor_ref
        self._rebuild_team = rebuild_team
        self._fallback = FallbackSummariser(lambda: source.team.model)
        self._reporter = ContextBreakdownReporter(source)

    async def compact(self) -> CompactResult:
        """Generate a summary of the conversation, then clear old runs.

        1. Generate summary covering the full conversation via
           Agno's structured summariser.
        2. If that returns empty, fall back to the free-text
           summariser (MiniMax workaround — see
           :class:`FallbackSummariser`).
        3. If neither produced a *new* summary, stop here and keep
           the conversation — see the abort branch below.
        4. Delete all runs from the session (summary preserved).
        5. Rebuild the main agent so its in-memory message cache
           is empty and the next turn sees the summary as system
           context.

        Returns a :class:`CompactResult` — ``ok`` is ``True`` when
        a non-empty summary landed; ``error`` carries the
        summariser exception when the pass failed;
        ``history_cleared`` says whether the runs actually left the
        DB, which is what callers must mirror.
        """
        agno_session = await self._source.load()
        if agno_session is None:
            logger.warning("No session found to compact")
            return CompactResult(
                ok=False,
                status="Session not found in DB",
                error="Session not found in DB",
            )

        # What the session already carried before this pass. A summariser
        # that fails leaves the PREVIOUS summary sitting on the object, and
        # that text says nothing about the runs this pass is about to drop —
        # so an unchanged summary counts as "no summary", not as a success.
        previous_summary = getattr(getattr(agno_session, "summary", None), "summary", None)

        summariser_error: str | None = None
        try:
            ssm = SessionSummaryManager(model=self._source.team.model)
            await ssm.acreate_session_summary(session=agno_session)
            logger.info("Session summary generated")
        except Exception as e:
            # Surface the underlying error so the UI can explain why the
            # summary is empty instead of just showing a generic placeholder.
            summariser_error = f"{type(e).__name__}: {e}"
            logger.warning("Failed to generate session summary: %s", e)

        summary_obj = getattr(agno_session, "summary", None)
        summary_text = getattr(summary_obj, "summary", None) if summary_obj else None
        if not summary_text or summary_text == previous_summary:
            try:
                summary_text = await self._fallback.summarise(agno_session)
            except Exception as e:
                logger.warning("Fallback summariser failed: %s", e)
                if not summariser_error:
                    summariser_error = f"fallback: {type(e).__name__}: {e}"
            else:
                if summary_text:
                    try:
                        agno_session.summary = SessionSummary(summary=summary_text)
                        logger.info(
                            "Session summary generated via fallback (%d chars)",
                            len(summary_text),
                        )
                    except Exception as e:
                        logger.warning("Could not attach fallback summary to session: %s", e)

        if not summary_text or summary_text == previous_summary:
            # Both summarisers came back empty. Clearing the runs here
            # deletes the entire conversation and puts NOTHING in its
            # place — and the trigger is a transient the user cannot
            # influence (provider 5xx, an unparseable response), firing
            # automatically at 80% context on every path that calls
            # ``compact_if_needed``. Keeping the history costs one more
            # over-threshold turn; dropping it costs the session.
            logger.warning(
                "Compaction aborted — no summary was produced, conversation kept (%s)",
                summariser_error or "summariser returned no text",
            )
            return CompactResult(
                ok=False,
                status=summariser_error or "",
                summary="",
                error=summariser_error,
                history_cleared=False,
            )

        agno_session.runs = []
        history_cleared = True
        try:
            # Agno 3 saves a session's row apart from its runs; the runs
            # are cleared from their own table (``run_history``).
            await RunHistory.save_for(self._source.team, agno_session)
            logger.info("Session runs cleared from DB")
        except Exception as e:
            # The DB still holds the runs, and the rebuilt agent reloads
            # them from there — so nothing was cleared, whatever the
            # in-memory object now says. Report that, or the FE clears a
            # transcript the next turn brings straight back.
            history_cleared = False
            logger.warning("Failed to save session: %s", e)

        # Rebuild main agent through the public seam.
        self._rebuild_team()
        logger.info("Compacted: summary injected, agent rebuilt")

        return CompactResult(
            ok=summariser_error is None and bool(summary_text),
            status=summariser_error or "",
            summary=summary_text or "",
            error=summariser_error,
            history_cleared=history_cleared,
        )

    async def compact_if_needed(self, input_tokens: int, context_window: int) -> bool:
        """Auto-compact at 80% context usage.

        Messages accumulate freely until context fills up. At 80%, a
        summary is generated and old turns are dropped. Returns
        ``True`` if compaction was applied.
        """
        if context_window <= 0 or input_tokens <= 0:
            return False

        usage = input_tokens / context_window
        if usage < self._COMPACT_THRESHOLD:
            return False

        # PreCompact hook — lets plugins export / summarise / cancel
        # before history is dropped. A blocking return cancels the
        # compaction (the auto trigger respects it; the user can
        # always retry manually).
        pre_payload = PreCompactHookPayload(
            session_id=self._source.session_id,
            scope="auto",
            tokens_before=input_tokens,
        )
        pre = await self._hook_executor_ref().execute(
            event=HookEvent.PRE_COMPACT.value,
            payload=pre_payload,
        )
        if not pre.should_continue:
            logger.info("Auto-compact cancelled by PreCompact hook: %s", pre.message)
            return False

        result = await self.compact()
        if not result.history_cleared:
            # Nothing was dropped, so nothing downstream should act as if
            # it was: PostCompact observers would export an unchanged
            # transcript and the caller would push ``SessionCleared`` to a
            # FE whose history is still there.
            logger.info(
                "Auto-compact at %.0f%% made no change: %s",
                usage * 100,
                result.status or "no summary produced",
            )
            return False
        logger.info("Auto-compacted at %.0f%% context usage", usage * 100)
        # PostCompact hook — observation only; can't undo at this point.
        post_payload = PostCompactHookPayload(
            session_id=self._source.session_id,
            scope="auto",
            tokens_before=input_tokens,
        )
        await self._hook_executor_ref().execute(
            event=HookEvent.POST_COMPACT.value,
            payload=post_payload,
        )
        return True

    async def force_compact(self) -> CompactResult:
        """Manually compact conversation context.

        Returns a :class:`CompactResult` — ``status`` carries the
        human-readable status line for the ``/compact`` card,
        ``summary`` carries the generated summary text (empty on
        the failure branches), and ``error`` carries the
        underlying summariser exception when the pass failed.
        """
        # Check if there's anything to compact.
        try:
            agno_session = await self._source.load()
            if agno_session is None or not agno_session.runs:
                return CompactResult(
                    ok=False,
                    status="Nothing to compact — no conversation history.",
                    summary="",
                )
        except Exception as exc:
            logger.warning("force_compact pre-check aget_session failed: %s", exc)

        # PreCompact hook (manual /compact path). Honouring the blocking
        # decision: the user invoked the command but a plugin can still
        # veto (e.g. an unsaved-changes guard).
        pre_payload = PreCompactHookPayload(
            session_id=self._source.session_id,
            scope="manual",
            tokens_before=0,
        )
        pre = await self._hook_executor_ref().execute(
            event=HookEvent.PRE_COMPACT.value,
            payload=pre_payload,
        )
        if not pre.should_continue:
            return CompactResult(
                ok=False,
                status=pre.message or "Compaction cancelled by PreCompact hook.",
                summary="",
            )

        inner = await self.compact()
        error = inner.error

        if not inner.history_cleared:
            # The summariser produced nothing, so ``compact`` kept the
            # conversation. Say that plainly — the old wording ("Context
            # cleared, but …") described a deletion, and re-reading the
            # summary from the DB here would find the STALE one and
            # report a success. PostCompact does not fire: there is no
            # history-cleared event to observe.
            status = (
                f"Compaction aborted — your conversation is untouched. Summariser failed: {error}"
                if error
                else (
                    "Compaction aborted — the summariser returned no text "
                    "(the model may have returned an unparseable response). "
                    "Your conversation is untouched; try again."
                )
            )
            return CompactResult(
                ok=False,
                status=status,
                summary="",
                error=error,
                history_cleared=False,
            )

        # Retrieve the generated summary from DB.
        summary = ""
        try:
            agno_session = await self._source.load()
            if agno_session and agno_session.summary:
                summary = agno_session.summary.summary or ""
        except Exception as exc:
            logger.warning("force_compact post-fetch aget_session failed: %s", exc)

        if error and not summary:
            status = f"Context cleared, but the summariser failed: {error}"
            ok = False
        elif not summary:
            status = (
                "Context cleared, but the summariser returned no text "
                "(MiniMax may have returned an unparseable response)."
            )
            ok = False
        else:
            status = "Context compacted. Conversation summarized, history cleared."
            ok = True
        # PostCompact fires whether the summariser succeeded or not —
        # observers still need the history-cleared signal. (The pass that
        # cleared nothing returned above, before this point.)
        post_payload = PostCompactHookPayload(
            session_id=self._source.session_id,
            scope="manual",
            tokens_before=0,
            summary_chars=len(summary),
        )
        await self._hook_executor_ref().execute(
            event=HookEvent.POST_COMPACT.value,
            payload=post_payload,
        )
        return CompactResult(
            ok=ok, status=status, summary=summary, error=error, history_cleared=True
        )

    async def context_breakdown(self) -> ContextBreakdown:
        """Delegate to :meth:`ContextBreakdownReporter.report`."""
        return await self._reporter.report()
