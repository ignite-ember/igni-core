"""RPC handlers that discover, run and cancel ``.igni/workflows/*.mjs``.

The work is the :class:`WorkflowRunner`'s; these handlers read the
request model, delegate, and return the response model. The runner
is a context collaborator: the :class:`RpcRouter` builds one per
session and hands it over on :class:`RpcHandlerContext`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from igni_core.backend.rpc_handlers.base import RpcHandler, rpc
from igni_core.backend.schemas_workflows import (
    WorkflowCancelRequest,
    WorkflowCancelResponse,
    WorkflowListItem,
    WorkflowRunRequest,
    WorkflowRunResponse,
)
from igni_core.protocol.rpc import RpcMethod

if TYPE_CHECKING:
    from igni_core.backend.workflow_runner import WorkflowRunner


class WorkflowsRpcHandler(RpcHandler):
    """Discover, run and cancel workflows in this session.

    ``run_workflow`` returns the ``workflow_run_id`` at once; the FE
    folds the ``workflow_event`` pushes that follow into a chat item
    keyed by that id. RPCs arrive already routed to their session, so
    the run belongs to ``ctx.backend.session``.
    """

    @property
    def _runner(self) -> WorkflowRunner:
        runner = self._ctx.workflows
        if runner is None:
            raise RuntimeError("RpcHandlerContext built without a WorkflowRunner")
        return runner

    @rpc(RpcMethod.LIST_WORKFLOWS)
    async def list_workflows(self, args: dict) -> list[WorkflowListItem]:
        return [env.to_list_item() for env in await self._runner.list_workflows()]

    @rpc(RpcMethod.RUN_WORKFLOW)
    async def run_workflow(self, args: dict) -> WorkflowRunResponse:
        request = WorkflowRunRequest.from_rpc(args)
        result = await self._runner.run(request=request, session=self._ctx.backend.session)
        if not result.ok:
            raise result.to_exception()
        return WorkflowRunResponse(workflow_run_id=result.workflow_run_id, name=request.name)

    @rpc(RpcMethod.CANCEL_WORKFLOW)
    async def cancel_workflow(self, args: dict) -> WorkflowCancelResponse:
        """Stop a running workflow. The run sends the subprocess a
        :class:`WorkflowCancelCommand` and escalates to SIGTERM after
        a grace period.

        ``cancelled`` is false when no such run is active in this
        session — already completed, already cancelled, or unknown.
        """
        request = WorkflowCancelRequest.from_rpc(args)
        cancelled = await self._runner.cancel(request.workflow_run_id)
        return WorkflowCancelResponse(cancelled=cancelled, workflow_run_id=request.workflow_run_id)
