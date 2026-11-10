"""Plan-mode RPC handlers (latest / approve / dismiss)."""

from __future__ import annotations

from pydantic import ConfigDict, Field

from igni_core.backend.rpc_binding import Correlation, NoArgs, RpcArgs, rpc
from igni_core.backend.rpc_handlers.base import RpcHandler
from igni_core.backend.schemas_plan import LatestPlanResult
from igni_core.core.session.schemas import PlanDecisionResult
from igni_core.protocol.rpc import RpcMethod, RpcScope
from igni_core.protocol.schemas.enums import PushChannel


class PlanRunArgs(RpcArgs):
    """Arguments naming the run that submitted a plan.

    An empty ``run_id`` is refused in the answer (``ok: false``) rather
    than as an invalid argument; a number is taken as its string."""

    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)

    run_id: str = Field(
        default="",
        description="Id of the run that submitted the plan, as `plan_submitted` carries it.",
    )


class PlanRpcHandler(RpcHandler):
    """Latest snapshot + approve + dismiss."""

    @rpc(RpcMethod.GET_LATEST_PLAN, args=NoArgs, scope=RpcScope.SESSION)
    def get_latest_plan(self, args: NoArgs) -> LatestPlanResult:
        """Read the plan the agent last submitted in this session, with its task list.

        Takes no arguments. Returns the ``latest`` plan's text, every plan
        submitted before it (``history``), the to-do list (``tasks``), and
        ``state``: ``pending`` while a plan is waiting for a decision, empty
        when none was submitted.
        """
        return self._ctx.backend.get_latest_plan()

    @rpc(
        RpcMethod.APPROVE_PLAN,
        correlate=Correlation(by="payload.run_id", source="args.run_id"),
        args=PlanRunArgs,
        scope=RpcScope.SESSION,
        streams=(PushChannel.PERMISSION_MODE_CHANGED, PushChannel.PLAN_DECIDED),
    )
    async def approve_plan(self, args: PlanRunArgs) -> PlanDecisionResult:
        """Approve the plan the agent submitted, so it leaves plan mode and carries it out.

        ``run_id`` is the run that submitted the plan (``plan_submitted``
        carries it). The decision is recorded, the session's permission
        mode returns to the one it started in (``permission_mode_changed``
        when it changes), and ``plan_decided`` tells every window. Returns
        the ``run_id``, the ``decision``, ``mode_status`` describing the
        mode change, and ``ok``; ``ok: false`` with ``error`` when
        ``run_id`` is empty or the decision could not be recorded.
        """
        return await self._ctx.backend.session.plan.approve(run_id=args.run_id)

    @rpc(
        RpcMethod.DISMISS_PLAN,
        correlate=Correlation(by="payload.run_id", source="args.run_id"),
        args=PlanRunArgs,
        scope=RpcScope.SESSION,
        streams=(PushChannel.PLAN_DECIDED,),
    )
    async def dismiss_plan(self, args: PlanRunArgs) -> PlanDecisionResult:
        """Send the submitted plan back for refining, staying in plan mode.

        ``run_id`` is the run that submitted the plan. The decision is
        recorded and ``plan_decided`` tells every window. Returns the same
        shape as ``approve_plan``, with an empty ``mode_status``; ``ok:
        false`` with ``error`` when ``run_id`` is empty.
        """
        return await self._ctx.backend.session.plan.dismiss(run_id=args.run_id)
