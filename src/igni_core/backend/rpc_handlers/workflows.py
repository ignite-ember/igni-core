"""RPC handlers that discover, run and cancel ``.igni/workflows/*.mjs``.

The work is the :class:`~igni_core.backend.workflow_runner.WorkflowRunner`'s;
these handlers read the request model, delegate, and return the response
model. The runner is a context collaborator: the :class:`RpcRouter` builds
one per session and hands it over on :class:`RpcHandlerContext`.
"""

from __future__ import annotations

from typing import Any

from igni_core.backend.rpc_handlers.base import RpcHandler, rpc
from igni_core.backend.schemas_workflows import (
    WorkflowCancelRequest,
    WorkflowCancelResponse,
    WorkflowListItem,
    WorkflowListRequest,
    WorkflowRunRequest,
    WorkflowRunResponse,
)
from igni_core.protocol.rpc import RpcMethod


class WorkflowsRpcHandler(RpcHandler):
    """Discover, run and cancel workflows in this session.

    ``run_workflow`` returns the ``workflow_run_id`` at once; the FE
    folds the ``workflow_event`` pushes that follow into a chat item
    keyed by that id. RPCs arrive already routed to their session, so
    the run belongs to ``ctx.backend.session``.
    """

    @rpc(RpcMethod.LIST_WORKFLOWS)
    async def list_workflows(self, args: dict[str, Any]) -> list[WorkflowListItem]:
        WorkflowListRequest.from_rpc(args)
        return [env.to_list_item() for env in await self._ctx.workflows.list_workflows()]

    @rpc(RpcMethod.RUN_WORKFLOW)
    async def run_workflow(self, args: dict[str, Any]) -> WorkflowRunResponse:
        """Start a run and return its id; a miss raises (see
        :meth:`WorkflowStartResult.to_response`)."""
        request = WorkflowRunRequest.from_rpc(args)
        # The session this call was routed to, read per call rather than bound at build.
        result = await self._ctx.workflows.run(request=request, session=self._ctx.backend.session)
        return result.to_response()

    @rpc(RpcMethod.CANCEL_WORKFLOW)
    async def cancel_workflow(self, args: dict[str, Any]) -> WorkflowCancelResponse:
        """Stop a running workflow. The run sends the subprocess a
        :class:`WorkflowCancelCommand` and escalates to SIGTERM after
        a grace period.

        ``cancelled`` is false when no such run is active in this
        session — already completed, already cancelled, or unknown.
        """
        request = WorkflowCancelRequest.from_rpc(args)
        return request.answer(await self._ctx.workflows.cancel(request.workflow_run_id))
