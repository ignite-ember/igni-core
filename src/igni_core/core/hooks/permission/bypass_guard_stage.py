"""Bypass mode's last check, as a pipeline stage."""

from __future__ import annotations

from igni_core.core.hooks.bypass_guard import BypassGuard
from igni_core.core.hooks.permission.context import ToolCallContext
from igni_core.core.hooks.permission.outcome import Block, Continue, StageOutcome
from igni_core.core.hooks.permission.stage import PermissionStage


class BypassGuardStage(PermissionStage):
    """Puts a possibly harmful Bypass-allowed call to the person — see
    :mod:`~igni_core.core.hooks.bypass_guard`.

    Last in the pipeline, so an explicit deny, a protected path or a
    blocked command wins first; a hook's ``allow`` skips it.
    """

    skipped_by_hook_allow = True

    def __init__(self, guard: BypassGuard) -> None:
        self._guard = guard

    async def evaluate(self, ctx: ToolCallContext) -> StageOutcome:
        message = await self._guard.check(ctx.name, ctx.args, ctx.agent_path)
        return Block(message) if message else Continue()
