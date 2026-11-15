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
from igni_core.core.hooks.permission.outcome import AllowSkip, Block, Continue, PermissionVerdict
from igni_core.core.hooks.permission.pre_tool_use_stage import PreToolUseHookStage
from igni_core.core.hooks.permission.protected_path_stage import ProtectedPathStage
from igni_core.core.hooks.permission.stage import PermissionStage
from igni_core.core.utils.tool_audit import DecidedBy


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
        return (await self.decide(ctx)).message

    async def decide(self, ctx: ToolCallContext) -> PermissionVerdict:
        """The pipeline's decision, and what made it.

        A block is decided by the stage that blocked. A call that runs is
        decided by the last stage that positively allowed it — a hook's
        ``allow``, a rule, a mode, the person — and by
        :attr:`DecidedBy.POLICY` when none did.
        """
        hook_allowed = False
        allowed_by = DecidedBy.POLICY
        reason = ""
        for stage in self._stages:
            if hook_allowed and stage.skipped_by_hook_allow:
                continue
            outcome = await stage.evaluate(ctx)
            if isinstance(outcome, Block):
                return PermissionVerdict(outcome.message, outcome.by, outcome.reason)
            if isinstance(outcome, AllowSkip):
                hook_allowed = True
                allowed_by, reason = DecidedBy.HOOK, ""
            elif isinstance(outcome, Continue) and outcome.by is not None:
                allowed_by, reason = outcome.by, outcome.reason
        return PermissionVerdict(None, allowed_by, reason)
