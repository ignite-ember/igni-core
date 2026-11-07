"""Workflow runner — run ``.igni/workflows/*.mjs`` through one RPC router.

:class:`WorkflowRunner` starts, cancels and closes the runs started
through one :class:`RpcRouter`. It finds a workflow with
:class:`WorkflowDiscovery`, spawns it with :class:`WorkflowRuntime`, and
admits the resulting :class:`WorkflowRun` to a
:class:`WorkflowRunRegistry`, which keeps it until it ends.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from igni_core.backend.schemas_workflows import (
    WorkflowCancelOutcome,
    WorkflowMetaEnvelope,
    WorkflowRunIdentity,
    WorkflowRunRequest,
    WorkflowStartResult,
)
from igni_core.backend.workflow_discovery import WorkflowDiscovery
from igni_core.backend.workflow_run import WorkflowRun
from igni_core.backend.workflow_run_registry import WorkflowRunRegistry
from igni_core.backend.workflow_runtime_process import RUNTIME_PATH, WorkflowRuntime
from igni_core.core.activity.background import BackgroundTasks

if TYPE_CHECKING:
    from igni_core.backend.push_bridge import PushNotificationBridge
    from igni_core.core.session.core import Session


class WorkflowRunner:
    """Start, cancel and close the workflow runs of one router.

    Each :class:`RpcRouter` builds one and hands it to the RPC handlers on
    :class:`RpcHandlerContext`. The session a run belongs to is given per
    call, because each RPC is routed to its session as it arrives.
    """

    def __init__(
        self,
        *,
        discovery: WorkflowDiscovery,
        runtime: WorkflowRuntime,
        push: PushNotificationBridge,
        registry: WorkflowRunRegistry,
    ) -> None:
        self._discovery = discovery
        self._runtime = runtime
        self._push = push
        self._registry = registry

    @classmethod
    def for_project(cls, *, project_dir: Path, push: PushNotificationBridge) -> WorkflowRunner:
        runtime = WorkflowRuntime(runtime_path=RUNTIME_PATH, cwd=project_dir)
        return cls(
            discovery=WorkflowDiscovery.for_project(project_dir, runtime=runtime),
            runtime=runtime,
            push=push,
            registry=WorkflowRunRegistry(tasks=BackgroundTasks.shared()),
        )

    async def list_workflows(self) -> list[WorkflowMetaEnvelope]:
        return await self._discovery.list_workflows()

    def is_live(self, workflow_run_id: str) -> bool:
        return workflow_run_id in self._registry

    def has_live_runs(self) -> bool:
        return len(self._registry) > 0

    async def run(self, *, request: WorkflowRunRequest, session: Session) -> WorkflowStartResult:
        """Start a run and return its id at once; its events follow as pushes.

        The FE creates a ``ChatItem`` keyed by the id and folds the
        ``workflow_event`` pushes into it.
        """
        envelope = await self._discovery.resolve(request.name)
        if envelope is None:
            return self._discovery.refuse_missing(request.name)
        refusal = self._runtime.preflight()
        if refusal is not None:
            return refusal

        identity = WorkflowRunIdentity.new(request.name)
        process = await self._runtime.start(
            identity, envelope, session_id=session.id, args=request.args
        )
        self._registry.admit(
            WorkflowRun.for_session(
                identity=identity, process=process, session=session, push=self._push
            )
        )
        return WorkflowStartResult.started(identity)

    async def cancel(self, workflow_run_id: str) -> WorkflowCancelOutcome:
        return await self._registry.cancel(workflow_run_id)

    async def close(self) -> None:
        """Cancel every live run and wait for each to end."""
        await self._registry.cancel_all()
