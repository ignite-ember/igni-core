"""The live workflow runs of one router, and the tasks that supervise them.

:class:`WorkflowRunRegistry` admits a started :class:`WorkflowRun`, keeps
it until its process exits and its terminal record is written, and is
the one place a run is looked up to be cancelled. Closing it cancels
every live run and waits for each to end.
"""

from __future__ import annotations

import asyncio
import logging

from igni_core.backend.schemas_workflows import WorkflowCancelOutcome
from igni_core.backend.workflow_run import DRAIN_FLUSH_SECONDS, WorkflowRun
from igni_core.backend.workflow_runtime_process import (
    CANCEL_GRACE_SECONDS,
    TERMINATE_GRACE_SECONDS,
)
from igni_core.core.activity.background import BackgroundTasks

logger = logging.getLogger(__name__)

#: How long :meth:`WorkflowRunRegistry.cancel_all` waits for the runs to
#: reach their terminal records: a stop's whole escalation, the drain's
#: flush, and a margin for the record's persist.
CLOSE_WAIT_SECONDS = CANCEL_GRACE_SECONDS + TERMINATE_GRACE_SECONDS + DRAIN_FLUSH_SECONDS + 5.0


class WorkflowRunRegistry:
    """Owns the live runs, by id, and the task supervising each."""

    def __init__(self, *, tasks: BackgroundTasks) -> None:
        self._tasks = tasks
        self._runs: dict[str, WorkflowRun] = {}
        self._supervisors: dict[str, asyncio.Task[None]] = {}

    def __contains__(self, workflow_run_id: object) -> bool:
        return workflow_run_id in self._runs

    def __len__(self) -> int:
        return len(self._runs)

    def admit(self, run: WorkflowRun) -> None:
        """Start draining ``run`` and keep it, busy, until it ends."""
        run_id = run.identity.workflow_run_id
        run.start_drain()
        supervisor = self._tasks.start(
            self._supervise(run),
            holder=f"workflow:{run_id}",
            reason="a workflow",
            name=f"workflow-await-{run_id}",
        )
        # Recorded only once the task exists, so a failed start leaves no entry.
        self._runs[run_id] = run
        self._supervisors[run_id] = supervisor

    async def cancel(self, workflow_run_id: str) -> WorkflowCancelOutcome:
        run = self._runs.get(workflow_run_id)
        if run is None:
            return WorkflowCancelOutcome.NOT_LIVE
        return await run.cancel()

    async def cancel_all(self) -> None:
        """Cancel every live run at once, then wait for each to end."""
        runs = list(self._runs.values())
        supervisors = list(self._supervisors.values())
        outcomes = await asyncio.gather(*(run.cancel() for run in runs), return_exceptions=True)
        for run, outcome in zip(runs, outcomes, strict=True):
            # Each stop is its own task boundary: one that fails must not
            # leave the other runs' processes going, so it is logged here.
            if isinstance(outcome, BaseException):
                logger.error(
                    "error cancelling workflow %s",
                    run.identity.workflow_run_id,
                    exc_info=outcome,
                )
        if not supervisors:
            return
        _done, pending = await asyncio.wait(supervisors, timeout=CLOSE_WAIT_SECONDS)
        for supervisor in pending:
            logger.warning("workflow %s did not end on close", supervisor.get_name())

    async def _supervise(self, run: WorkflowRun) -> None:
        run_id = run.identity.workflow_run_id
        try:
            await run.finish()
        finally:
            self._runs.pop(run_id, None)
            self._supervisors.pop(run_id, None)
