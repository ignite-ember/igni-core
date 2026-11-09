"""RPC handlers that discover, run and cancel ``.igni/workflows/*.mjs``.

The work is the :class:`~igni_core.backend.workflow_runner.WorkflowRunner`'s;
these handlers delegate and return the response model. The runner is a
context collaborator: each :class:`RpcRouter` builds one and hands it over
on :class:`RpcHandlerContext`.
"""

from __future__ import annotations

from igni_core.backend.rpc_binding import rpc
from igni_core.backend.rpc_handlers.base import RpcHandler
from igni_core.backend.schemas_workflows import (
    WorkflowCancelRequest,
    WorkflowCancelResponse,
    WorkflowListItem,
    WorkflowListRequest,
    WorkflowRunRequest,
    WorkflowRunResponse,
)
from igni_core.protocol.rpc import RpcDuration, RpcMethod, RpcScope
from igni_core.protocol.schemas.enums import PushChannel


class WorkflowsRpcHandler(RpcHandler):
    """Discover, run and cancel workflows in this session.

    ``run_workflow`` returns the ``workflow_run_id`` at once; the FE
    folds the ``workflow_event`` pushes that follow into a chat item
    keyed by that id. RPCs arrive already routed to their session, so
    the run belongs to ``ctx.backend.session``.
    """

    @rpc(
        RpcMethod.LIST_WORKFLOWS,
        duration=RpcDuration.IO,
        args=WorkflowListRequest,
        scope=RpcScope.SESSION,
    )
    async def list_workflows(self, args: WorkflowListRequest) -> list[WorkflowListItem]:
        """List the workflows this session's project can run.

        Takes no arguments (any it is sent are ignored). Returns each workflow in
        ``.igni/workflows/*.mjs`` (a project's shadowing the person's own
        of the same name), sorted by name: its ``name``, ``description``,
        ``whenToUse``, ``phases`` and ``path``. A file whose metadata cannot
        be read is left out.
        """
        return [env.to_list_item() for env in await self._ctx.workflows.list_workflows()]

    @rpc(
        RpcMethod.RUN_WORKFLOW,
        duration=RpcDuration.IO,
        args=WorkflowRunRequest,
        scope=RpcScope.SESSION,
        streams=(PushChannel.WORKFLOW_EVENT,),
    )
    async def run_workflow(self, args: WorkflowRunRequest) -> WorkflowRunResponse:
        """Start a workflow in this session and return its run id at once.

        ``name`` is the workflow (from ``list_workflows``); ``args`` its
        input, a JSON object the workflow reads as it defines. Returns the
        ``workflow_run_id`` and ``name``. The run goes on in the
        background: each step it takes is pushed as ``workflow_event``
        carrying that id, ending with ``workflow_completed`` or
        ``workflow_failed``. Fails when there is no such workflow or the
        workflow runtime is missing.
        """
        # The session this call was routed to, read per call rather than bound at build.
        result = await self._ctx.workflows.run(request=args, session=self._ctx.backend.session)
        return result.to_response()

    @rpc(
        RpcMethod.CANCEL_WORKFLOW,
        duration=RpcDuration.IO,
        args=WorkflowCancelRequest,
        scope=RpcScope.SESSION,
        streams=(PushChannel.WORKFLOW_EVENT,),
    )
    async def cancel_workflow(self, args: WorkflowCancelRequest) -> WorkflowCancelResponse:
        """Stop a running workflow of this session.

        ``workflow_run_id`` is the run (from ``run_workflow``). The
        workflow is asked to stop, then terminated after a grace period;
        its last ``workflow_event`` says it was cancelled. Returns
        ``cancelled: true`` when a live run was stopped, ``false`` when
        there was no such run or it had already ended, with the
        ``workflow_run_id`` asked about.
        """
        return args.answer(await self._ctx.workflows.cancel(args.workflow_run_id))
