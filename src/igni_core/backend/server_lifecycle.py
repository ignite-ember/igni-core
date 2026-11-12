"""Backend lifecycle — startup, interrupted-run detection, shutdown.

Class-first module: :class:`LifecycleController` is the only entry
point. Startup / detect_interrupted_run / shutdown are typed
methods on it.

* :meth:`LifecycleController.startup` — awaited post-``__init__``
  hook. Delegates the rehydrate summary log to
  :class:`RehydrateOutcomeSet.log` so the outcomes own their
  logging ceremony.
* :meth:`LifecycleController.detect_interrupted_run` — delegates
  to :class:`InterruptedRunSummaryBuilder` for the actual
  assembly and hands a typed :class:`InterruptedRunSummary` to
  :meth:`RunController.set_interrupted_summary`.
* :meth:`LifecycleController.shutdown` — delegates to a
  :class:`ShutdownPipeline` built once in ``__init__``. Cleanup
  failures land on the returned :class:`ShutdownResult` (typed
  ``errors`` list) rather than getting swallowed by
  ``contextlib.suppress`` blocks.

Every collaborator (run controller, pending store, rehydrate
controller, message queue) is constructor-injected; the controller
holds no reference back to ``BackendServer``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from igni_core.backend.schemas_lifecycle import (
    InterruptedRunSummary,
    RehydrateOutcomeSet,
    ShutdownResult,
)
from igni_core.backend.server_interrupted_run import InterruptedRunSummaryBuilder
from igni_core.backend.server_shutdown import ShutdownPipeline

if TYPE_CHECKING:
    from igni_core.backend.run_controller import RunController
    from igni_core.backend.server_rehydrate import RehydrateController
    from igni_core.core.session import Session
    from igni_core.core.session.pending_messages import PendingMessageStore
    from igni_core.core.session.queued_messages import DurableMessageQueue

logger = logging.getLogger(__name__)


class LifecycleController:
    """Startup / shutdown / interrupted-run detection for one BackendServer."""

    def __init__(
        self,
        session: Session,
        pending_store: PendingMessageStore | None,
        runs: RunController | None,
        rehydrate: RehydrateController,
        scheduler_stop: Callable[[], Awaitable[None]] | None = None,
        message_queue: DurableMessageQueue | None = None,
    ) -> None:
        self._session = session
        self._pending_store = pending_store
        self._runs = runs
        self._rehydrate = rehydrate
        self._message_queue = message_queue
        self._summary_builder = InterruptedRunSummaryBuilder(
            session=session,
            pending_store=pending_store,
        )
        # Shutdown pipeline is data on the controller, not a closure
        # blob in a method body. Scheduler-stop is threaded in here
        # so the pipeline decides once (at build time) whether to
        # include :class:`SchedulerStopStep`.
        self._shutdown_pipeline = ShutdownPipeline(
            session=session,
            scheduler_stop=scheduler_stop,
        )
        # The first teardown's outcome. A session is torn down once: the
        # ``shutdown`` call, the pool evicting it and igni exiting can each
        # ask, and ``SessionEnd`` must not fire twice.
        # Callers that arrive while the first is still tearing down wait for
        # it, rather than starting a second teardown of their own.
        self._shutdown: ShutdownResult | None = None
        self._shutdown_lock = asyncio.Lock()

    async def startup(self) -> None:
        """Async post-construction hook.

        Hydrates the persisted ``/loop`` state, probes for an
        interrupted-run signal, and rehydrates the five persisted
        state roots (plan store, plan decisions, todos, event log,
        orphan processes) in a documented order — plan_store seeds
        first, todos overlays. Each rehydrate step returns a typed
        :class:`RehydrateOutcome`; the batch is handed to
        :class:`RehydrateOutcomeSet` which owns the summary log.
        """
        await self._session.loop.load_persisted_loop_state()
        await self.detect_interrupted_run()
        outcomes = RehydrateOutcomeSet.of(
            await self._rehydrate.plan_store(),
            await self._rehydrate.plan_decisions(),
            await self._rehydrate.todos(),
            await self._rehydrate.event_log(),
            await self._rehydrate.orphan_processes(),
            await self._rehydrate.queued_messages(self._message_queue),
        )
        outcomes.log(logger)

    async def detect_interrupted_run(self) -> InterruptedRunSummary | None:
        """Build a system-context note if the previous launch crashed
        mid-run.

        Delegates assembly to :class:`InterruptedRunSummaryBuilder`
        and hands the typed :class:`InterruptedRunSummary` to
        :meth:`RunController.set_interrupted_summary`. Returns the
        summary so tests can assert on the shape without reaching
        through the run controller.
        """
        summary = await self._summary_builder.build()
        if summary is not None and self._runs is not None:
            self._runs.set_interrupted_summary(summary)
        return summary

    async def shutdown(self) -> ShutdownResult:
        """Graceful shutdown — fire hooks, clean the agent pool, disconnect
        MCP, stop the scheduler. Once: a later call answers the first
        teardown's outcome and runs nothing again. Background processes
        are igni's, and end when igni does (``BackendSupervisor.teardown``).

        Returns a typed :class:`ShutdownResult` so cleanup failures
        are observable outcomes rather than silent swallows. The
        pipeline was built once in :meth:`__init__`; each step is
        best-effort (an exception in one step is recorded and the
        runner moves on).
        """
        async with self._shutdown_lock:
            if self._shutdown is None:
                self._shutdown = await self._shutdown_pipeline.run()
            return self._shutdown
