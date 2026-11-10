"""``host.workflows``: the workflows, and the runs they start."""

from __future__ import annotations

from igni_core.backend.schemas_workflows import (
    WorkflowCancelResponse,
    WorkflowListItem,
    WorkflowRunResponse,
)
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.reads import read_answer, read_answers
from igni_core.extensions.host.members.tag import call, get
from igni_core.extensions.host.rpc_wire.work import CancelWorkflowArgs, RunWorkflowArgs
from igni_core.protocol.rpc import RpcMethod


class WorkflowMembers(Members):
    @get("workflows.list")
    async def workflow_list(self, c: Call) -> list[WorkflowListItem]:
        return read_answers(
            WorkflowListItem,
            await self.rpc(RpcMethod.LIST_WORKFLOWS, c=c),
            method="workflows.list",
        )

    @call("workflows.run")
    async def run(self, c: Call) -> WorkflowRunResponse:
        args = RunWorkflowArgs(
            name=c.params["name"], args=c.params.get("input") or {}, session_id=c.session_id
        )
        return read_answer(
            WorkflowRunResponse,
            await self.rpc(RpcMethod.RUN_WORKFLOW, args, c=c),
            method="workflows.run",
        )

    @call("workflows.cancel")
    async def cancel(self, c: Call) -> bool:
        args = CancelWorkflowArgs(workflow_run_id=c.params["workflow_run_id"])
        found = read_answer(
            WorkflowCancelResponse,
            await self.rpc(RpcMethod.CANCEL_WORKFLOW, args, c=c),
            method="workflows.cancel",
        )
        return found.cancelled


__all__ = ["WorkflowMembers"]
