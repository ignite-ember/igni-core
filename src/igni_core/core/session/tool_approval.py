"""Putting one tool call to the person, on the in-run approval channel.

The Bypass guard, the group-hook gate and the permission pipeline all ask
about a single call the same way: a requirement shaped like an Agno
``RunRequirement`` is pushed to the session's
:class:`~igni_core.core.sub_agent_hitl.SubAgentHITLCoordinator`, the
backend relays it as an ordinary approval dialog, and the answer comes
back through ``HitlController``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

from igni_core.core.activity import background
from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator

FollowUp = Callable[[], Awaitable[str | None]]


class GuardToolExecution(BaseModel):
    """The two fields the approval dialog reads from a requirement's tool execution."""

    tool_name: str
    # Tool arguments are open JSON by nature.
    tool_args: dict[str, Any] = Field(default_factory=dict)


class GuardRequirement(BaseModel):
    """A single-call question shaped like an Agno ``RunRequirement``.

    Carries only what the coordinator and the dialog touch.
    """

    tool_execution: GuardToolExecution
    details: str
    #: The model is still describing the call; the dialog spins.
    details_pending: bool = False
    confirmed: bool = False

    def confirm(self) -> None:
        self.confirmed = True

    def reject(self, note: str = "") -> None:
        self.confirmed = False


class ToolApprovalRequest(BaseModel):
    """One tool call to put to the person."""

    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    reason: str
    agent_path: list[str] = Field(default_factory=list)


class ToolCallApprovalChannel:
    """Asks the person about one tool call; True / False, or None if nobody can answer."""

    def __init__(self, hitl: SubAgentHITLCoordinator) -> None:
        self._hitl = hitl

    async def ask(
        self,
        tool_name: str,
        args: dict[str, Any],
        reason: str,
        agent_path: list[str],
        *,
        follow_up: FollowUp | None = None,
    ) -> bool | None:
        """The ``AskPerson`` / ``Ask`` / ``AskAboutGroupHook`` contract.

        With ``follow_up`` the dialog opens with a spinner instead of text,
        and shows the model's description when it comes — or ``reason``
        when it does not. Answering first cancels it.
        """
        request = ToolApprovalRequest(
            tool_name=tool_name, args=args, reason=reason, agent_path=agent_path
        )
        return await self._put(request, follow_up)

    async def _put(self, request: ToolApprovalRequest, follow_up: FollowUp | None) -> bool | None:
        # With no relay running — a headless run — nobody can answer, and
        # waiting would hang the run for ever.
        hitl = self._hitl
        if hitl.listeners <= 0:
            return None
        requirement = GuardRequirement(
            tool_execution=GuardToolExecution(tool_name=request.tool_name, tool_args=request.args),
            details="" if follow_up else request.reason,
            details_pending=follow_up is not None,
        )
        req_id = await hitl.push_requirement(
            requirement,
            run_id=f"bypass-guard-{request.tool_name}",
            agent_path=[*request.agent_path, "safety check"],
        )

        async def explain_on_screen(explain: FollowUp) -> None:
            hitl.update_details(req_id, await explain() or request.reason)

        explaining = (
            background.start(
                explain_on_screen(follow_up), holder="approval", reason="explaining a call"
            )
            if follow_up
            else None
        )
        try:
            await hitl.wait_resolved(req_id)
        finally:
            if explaining is not None:
                explaining.cancel()
            hitl.cleanup(req_id)
        return requirement.confirmed


__all__ = [
    "GuardRequirement",
    "GuardToolExecution",
    "ToolApprovalRequest",
    "ToolCallApprovalChannel",
]
