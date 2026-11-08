"""The configured ``PreToolUse`` hooks, asked first."""

from __future__ import annotations

import logging

from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.hook_firer import HookFirer
from igni_core.core.hooks.permission.context import ToolCallContext
from igni_core.core.hooks.permission.outcome import AllowSkip, Block, Continue, StageOutcome
from igni_core.core.hooks.permission.stage import PermissionStage
from igni_core.core.hooks.schemas import PermissionDecision
from igni_core.core.hooks.tool_events import (
    PermissionDeniedPayload,
    PermissionRequestPayload,
    PreToolUsePayload,
)

logger = logging.getLogger(__name__)


class PreToolUseHookStage(PermissionStage):
    """Fires ``PreToolUse`` and reads the merged :class:`HookResult`.

    ``allow`` → :class:`AllowSkip`; ``deny``, ``ask`` or a legacy
    ``should_continue=False`` → :class:`Block`; otherwise :class:`Continue`.

    A hook's ``ask`` is refused: Agno's approval pause is driven by
    ``requires_confirmation``, which no hook can set, so nobody can be
    asked from here, and running the tool would turn a hook written to
    mean "prompt me" into a rubber stamp.
    """

    def __init__(self, firer: HookFirer) -> None:
        self._firer = firer

    async def evaluate(self, ctx: ToolCallContext) -> StageOutcome:
        result = await self._firer.fire(
            HookEvent.PRE_TOOL_USE,
            ctx.name,
            PreToolUsePayload.from_call(ctx.name, ctx.args),
        )
        decision = PermissionDecision.from_wire((result.permission_decision or "").lower())
        if decision is PermissionDecision.DENY:
            await self._fire_denied(ctx)
            return Block(result.message or f"Blocked: '{ctx.name}' denied by PreToolUse hook")
        if decision is PermissionDecision.ASK:
            return await self._refuse_ask(ctx, result.message)
        if decision is PermissionDecision.ALLOW:
            return AllowSkip()
        if not result.should_continue:
            return Block(result.message or "Blocked by PreToolUse hook")
        return Continue()

    async def _refuse_ask(self, ctx: ToolCallContext, message: str) -> Block:
        await self._firer.fire(
            HookEvent.PERMISSION_REQUEST,
            ctx.name,
            PermissionRequestPayload.from_call(ctx.name, ctx.args),
        )
        await self._fire_denied(ctx)
        logger.info(
            "PreToolUse hook returned ASK for %s — no prompt surface at this "
            "stage, so it is denied rather than auto-approved",
            ctx.name,
        )
        return Block(
            message
            or (
                f"Blocked: a PreToolUse hook asked for confirmation of '{ctx.name}', "
                "and hooks have no way to prompt the user. Ask the user directly, or "
                "have the hook return allow/deny."
            )
        )

    async def _fire_denied(self, ctx: ToolCallContext) -> None:
        await self._firer.fire(
            HookEvent.PERMISSION_DENIED,
            ctx.name,
            PermissionDeniedPayload.from_call(ctx.name, ctx.args, "pre_tool_use_hook"),
        )
