"""Boot-time state-recovery for one :class:`Session`.

Single-class module. :class:`RehydrateController` owns the five
best-effort recovery steps executed post-``BackendServer.__init__``
by :meth:`LifecycleController.startup`:

* :meth:`RehydrateController.event_log` — reload the append-only
  event log so ``get_session_events`` can serve it after restart.
* :meth:`RehydrateController.orphan_processes` — re-adopt any
  background shell processes that survived the previous BE
  lifetime.
* :meth:`RehydrateController.plan_decisions` — restore the
  ``{run_id: decision}`` map onto the live ``PlanStore``.
* :meth:`RehydrateController.todos` — overlay the persisted todo
  snapshot onto ``session.todo_store``.
* :meth:`RehydrateController.plan_store` — repopulate ``PlanStore``
  from the most recent ``exit_plan_mode`` tool call in history via
  :class:`AgnoHistoryPlanScanner`.

Every method returns a typed :class:`RehydrateOutcome` so
:meth:`LifecycleController.startup` can log a single structured
summary instead of each method silently swallowing at DEBUG.
Recovery stays best-effort throughout — no ``raise`` escapes the
controller.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from igni_core.backend.agno_history_plan_scanner import AgnoHistoryPlanScanner
from igni_core.backend.schemas_lifecycle import RehydrateOutcome
from igni_core.core.session.queued_messages import DurableMessageQueue
from igni_core.core.tools.orphan_rehydrator import build_rehydrator
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.tools.todo import TodoItemInput

if TYPE_CHECKING:
    from igni_core.core.session import Session

logger = logging.getLogger(__name__)


class RehydrateController:
    """Boot-time state-recovery for one :class:`Session`.

    Each method is a ~10-line best-effort loader that returns a
    typed :class:`RehydrateOutcome`; the lifecycle controller
    accumulates the five outcomes for a single summary log line.
    """

    def __init__(self, session: Session, supervisor: ProcessSupervisor) -> None:
        self._session = session
        # The supervisor the backend process shares — the registry the
        # orphaned processes are re-adopted onto.
        self._supervisor = supervisor

    async def event_log(self) -> RehydrateOutcome:
        """Load the persisted append-only event log onto the session."""
        persistence = self._session.persistence
        if persistence is None:
            return RehydrateOutcome(ok=True, step="event_log", reason="no persistence")
        try:
            entries = await persistence.load_event_log()
        except Exception as exc:
            return RehydrateOutcome(ok=False, step="event_log", reason=str(exc))
        if not entries:
            return RehydrateOutcome(ok=True, step="event_log", reason="empty on disk")
        self._session.restore_event_log(entries)
        return RehydrateOutcome(ok=True, step="event_log")

    async def queued_messages(self, queue: DurableMessageQueue | None) -> RehydrateOutcome:
        """Offer again what a run took and the conversation never recorded.

        The previous backend died between a run draining a queued message
        and the session being saved; the message goes back in the queue.
        One the conversation holds is dropped instead — delivered once.
        """
        if queue is None:
            return RehydrateOutcome(ok=True, step="queued_messages", reason="no queue")
        again = await queue.recover(self._session.db)
        return RehydrateOutcome(
            ok=True, step="queued_messages", reason=f"{again} offered again" if again else ""
        )

    async def orphan_processes(self) -> RehydrateOutcome:
        """Re-adopt every backgrounded shell process that survived the
        previous BE lifetime.

        Builds an :class:`OrphanRehydrator` (typed store-init
        failure surfaces via ``build.result.reason``) then
        plumbs the :class:`RehydrateResult.reason` straight into
        :class:`RehydrateOutcome` so failure modes stay
        observable at INFO instead of collapsing to a silent
        ``ok=True``.
        """
        supervisor = self._supervisor
        project_dir = self._session.project_dir
        supervisor.configure_log_store(project_dir)
        if project_dir is None:
            return RehydrateOutcome(ok=True, step="orphan_processes", reason="no project_dir")
        build = build_rehydrator(supervisor, project_dir)
        if build.rehydrator is None:
            # ``build.result`` carries the reason when the store
            # constructor raised — surface the typed reason
            # instead of the silent "return 0" the legacy wrapper
            # kept.
            reason = build.result.reason or "unknown"
            return RehydrateOutcome(ok=False, step="orphan_processes", reason=reason)
        try:
            result = await build.rehydrator.run()
        except Exception as exc:
            return RehydrateOutcome(ok=False, step="orphan_processes", reason=str(exc))
        return RehydrateOutcome(
            ok=result.ok,
            step="orphan_processes",
            reason=result.reason or None,
        )

    async def plan_decisions(self) -> RehydrateOutcome:
        """Load the ``{run_id: decision}`` map back into ``PlanStore``."""
        store = self._session.plan_store
        persistence = self._session.persistence
        if persistence is None:
            return RehydrateOutcome(ok=True, step="plan_decisions", reason="no persistence")
        try:
            blob = await persistence.load_plan_decisions()
        except Exception as exc:
            return RehydrateOutcome(ok=False, step="plan_decisions", reason=str(exc))
        # The blob comes back typed — the store's tolerant parser
        # already dropped a corrupt row on disk, so nothing untyped
        # reaches the decisions map.
        store.load_decisions(blob)
        return RehydrateOutcome(ok=True, step="plan_decisions")

    async def todos(self) -> RehydrateOutcome:
        """Load the persisted todo snapshot back into
        ``session.todo_store``.

        Order matters: this runs AFTER :meth:`plan_store` so it
        overwrites the plan-args seeding only when a real snapshot
        exists (i.e., execution has happened since the plan
        submission).
        """
        todo = self._session.todo_store
        persistence = self._session.persistence
        if persistence is None:
            return RehydrateOutcome(ok=True, step="todos", reason="no persistence")
        try:
            snapshot = await persistence.load_todos()
        except Exception as exc:
            return RehydrateOutcome(ok=False, step="todos", reason=str(exc))
        if not snapshot:
            # no live execution state yet; keep the plan-args seed
            return RehydrateOutcome(ok=True, step="todos", reason="no snapshot")
        # The rows come back typed — the store's coercion already
        # dropped a malformed row, so the snapshot replaces the
        # store's list wholesale.
        todo.set(snapshot)
        return RehydrateOutcome(ok=True, step="todos")

    async def plan_store(self) -> RehydrateOutcome:
        """Repopulate ``session.plan_store`` from the persisted history.

        Delegates the Agno history walk to
        :class:`AgnoHistoryPlanScanner` — the untyped-dict expedition
        lives behind that typed boundary, so this method stays a
        short applier that seeds :class:`PlanStore` and (optionally)
        :class:`TodoStore` from the resulting :class:`PlanArgs`.
        """
        store = self._session.plan_store
        if store.latest:
            return RehydrateOutcome(ok=True, step="plan_store", reason="already populated")
        try:
            agent = self._session.main_team
            agno_session = await agent.aget_session(
                session_id=self._session.session_id,
                user_id=self._session.user_id,
            )
        except Exception as exc:
            return RehydrateOutcome(ok=False, step="plan_store", reason=f"aget_session: {exc}")
        if agno_session is None:
            return RehydrateOutcome(ok=True, step="plan_store", reason="no agno session")
        scanner = AgnoHistoryPlanScanner(agno_session)
        found = scanner.find_latest_plan()
        if found is None:
            return RehydrateOutcome(ok=True, step="plan_store", reason="no plan in history")
        plan_args, run_id = found
        store.set_plan(plan_args.plan, run_id=run_id or "")
        if plan_args.tasks is not None:
            # The plan's rows go through the same validation the
            # todo tool uses — a row that cannot be used is
            # dropped, the rest seed the checklist.
            batch = TodoItemInput.validate_batch(plan_args.tasks)
            if batch.items:
                self._session.todo_store.set(batch.items)
        logger.info(
            "Rehydrated plan_store from history (run_id=%s, plan=%d chars)",
            run_id,
            len(plan_args.plan),
        )
        return RehydrateOutcome(ok=True, step="plan_store")
