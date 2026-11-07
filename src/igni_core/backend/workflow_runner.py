"""Workflow runner — run ``.igni/workflows/*.mjs`` for one session.

:class:`WorkflowRunner` keeps the session's live runs. It finds a workflow
with :class:`WorkflowDiscovery`, spawns it with :class:`WorkflowRuntime`,
and hands the process to a :class:`WorkflowRun`, which drains its events.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from igni_core.backend.schemas_workflows import (
    WorkflowMetaEnvelope,
    WorkflowRunIdentity,
    WorkflowRunRequest,
    WorkflowStartResult,
)
from igni_core.backend.workflow_agent_bridge import WorkflowAgentBridge
from igni_core.backend.workflow_discovery import WorkflowDiscovery
from igni_core.backend.workflow_run import WorkflowEventSink, WorkflowRun
from igni_core.backend.workflow_runtime_process import RUNTIME_PATH, WorkflowRuntime
from igni_core.core.activity import background

if TYPE_CHECKING:
    from igni_core.backend.push_bridge import PushNotificationBridge
    from igni_core.core.session.core import Session

logger = logging.getLogger(__name__)


class WorkflowRunner:
    """Start, cancel and track one session's workflow runs.

    Each :class:`RpcRouter` builds one for its session and hands it to the
    RPC handlers on :class:`RpcHandlerContext`.
    """

    def __init__(
        self,
        *,
        discovery: WorkflowDiscovery,
        runtime: WorkflowRuntime,
        push: PushNotificationBridge,
    ) -> None:
        self._discovery = discovery
        self._runtime = runtime
        self._push = push
        self._runs: dict[str, WorkflowRun] = {}

    @classmethod
    def for_project(
        cls,
        *,
        project_dir: Path,
        push: PushNotificationBridge,
        group_dir: Path | None = None,
    ) -> WorkflowRunner:
        runtime = WorkflowRuntime(runtime_path=RUNTIME_PATH, cwd=Path(project_dir))
        discovery = WorkflowDiscovery.for_project(
            Path(project_dir), group_dir=group_dir, runtime=runtime
        )
        return cls(discovery=discovery, runtime=runtime, push=push)

    async def list_workflows(self) -> list[WorkflowMetaEnvelope]:
        return await self._discovery.list_workflows()

    async def run(self, *, request: WorkflowRunRequest, session: Session) -> WorkflowStartResult:
        """Start a run and return its id at once; its events follow as pushes.

        The FE creates a ``ChatItem`` keyed by the id and folds the
        ``workflow_event`` pushes into it.
        """
        envelope = await self._discovery.resolve(request.name)
        if envelope is None:
            return WorkflowStartResult.not_found(self._discovery.not_found_message(request.name))
        if not self._runtime.available:
            return WorkflowStartResult.runtime_missing(self._runtime.missing_message)

        identity = WorkflowRunIdentity.new(request.name)
        process = await self._runtime.start(
            identity, envelope, session_id=session.id, args=request.args
        )
        run = WorkflowRun(
            identity=identity,
            process=process,
            sink=WorkflowEventSink(session, self._push),
            bridge=WorkflowAgentBridge(session),
        )
        run_id = identity.workflow_run_id
        self._runs[run_id] = run
        run.start_drain()
        # The workflow's script running: busy until it ends.
        background.start(
            self._supervise(run),
            holder=f"workflow:{run_id}",
            reason="a workflow",
            name=f"workflow-await-{run_id}",
        )
        return WorkflowStartResult.started(run_id)

    async def cancel(self, workflow_run_id: str) -> bool:
        """Stop a run. False when no such run is live."""
        run = self._runs.get(workflow_run_id)
        if run is None:
            return False
        return await run.cancel()

    async def shutdown(self) -> None:
        """Cancel every active run."""
        for run_id in list(self._runs):
            try:
                await self.cancel(run_id)
            except Exception:  # one failed cancel must not leave the other runs going
                logger.exception("error cancelling workflow %s", run_id)

    async def _supervise(self, run: WorkflowRun) -> None:
        try:
            await run.finish()
        finally:
            self._runs.pop(run.identity.workflow_run_id, None)
