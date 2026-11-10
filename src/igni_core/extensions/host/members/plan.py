"""``host.plan``: the plan the agent proposed, and what was decided about it."""

from __future__ import annotations

from igni_core.backend.schemas_plan import LatestPlanResult
from igni_core.core.session.schemas import PlanDecisionResult
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.reads import read_answer
from igni_core.extensions.host.members.tag import call, get
from igni_core.extensions.host.rpc_wire.chat import RunIdArgs
from igni_core.protocol.rpc import RpcMethod


class PlanMembers(Members):
    @get("plan.latest")
    async def latest(self, c: Call) -> LatestPlanResult:
        return read_answer(
            LatestPlanResult,
            await self.rpc(RpcMethod.GET_LATEST_PLAN, c=c),
            method="plan.latest",
        )

    @call("plan.approve")
    async def approve(self, c: Call) -> PlanDecisionResult:
        args = RunIdArgs(run_id=c.params.get("run_id") or "")
        return read_answer(
            PlanDecisionResult,
            await self.rpc(RpcMethod.APPROVE_PLAN, args, c=c),
            method="plan.approve",
        )

    @call("plan.dismiss")
    async def dismiss(self, c: Call) -> PlanDecisionResult:
        args = RunIdArgs(run_id=c.params.get("run_id") or "")
        return read_answer(
            PlanDecisionResult,
            await self.rpc(RpcMethod.DISMISS_PLAN, args, c=c),
            method="plan.dismiss",
        )


__all__ = ["PlanMembers"]
