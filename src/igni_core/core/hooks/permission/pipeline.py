"""The ordered runner over the permission stages."""

from __future__ import annotations

from collections.abc import Sequence

from igni_core.core.config.permission_eval import PermissionEvaluator
from igni_core.core.hooks.bypass_guard import BypassGuard
from igni_core.core.hooks.hook_firer import HookFirer
from igni_core.core.hooks.permission.blocked_command_stage import BlockedCommandStage
from igni_core.core.hooks.permission.bypass_guard_stage import BypassGuardStage
from igni_core.core.hooks.permission.context import ToolCallContext
from igni_core.core.hooks.permission.evaluator_stage import AskPerson, PermissionEvaluatorStage
from igni_core.core.hooks.permission.outcome import AllowSkip, Block
from igni_core.core.hooks.permission.pre_tool_use_stage import PreToolUseHookStage
from igni_core.core.hooks.permission.protected_path_stage import ProtectedPathStage
from igni_core.core.hooks.permission.stage import PermissionStage


class PermissionPipeline:
    """Runs its stages in order; the first :class:`Block` decides.

    After an :class:`AllowSkip` the stages a hook's ``allow`` may skip
    are skipped, and the rest still run.
    """

    def __init__(self, stages: Sequence[PermissionStage]) -> None:
        self._stages = tuple(stages)

    @classmethod
    def build(
        cls,
        firer: HookFirer,
        protected_paths: list[str],
        blocked_commands: list[str],
        permission_evaluator: PermissionEvaluator | None,
        bypass_guard: BypassGuard | None = None,
        ask_person: AskPerson | None = None,
    ) -> PermissionPipeline:
        """The CC-compatible order: hooks, the safety lists, the evaluator,
        and in Bypass the guard."""
        stages: list[PermissionStage] = [
            PreToolUseHookStage(firer),
            ProtectedPathStage(protected_paths),
            BlockedCommandStage(blocked_commands),
            PermissionEvaluatorStage(permission_evaluator, firer, ask_person),
        ]
        if bypass_guard is not None:
            stages.append(BypassGuardStage(bypass_guard))
        return cls(stages)

    async def evaluate(self, ctx: ToolCallContext) -> str | None:
        """The first block message, or ``None`` when every stage lets the call through."""
        hook_allowed = False
        for stage in self._stages:
            if hook_allowed and stage.skipped_by_hook_allow:
                continue
            outcome = await stage.evaluate(ctx)
            if isinstance(outcome, Block):
                return outcome.message
            if isinstance(outcome, AllowSkip):
                hook_allowed = True
        return None
