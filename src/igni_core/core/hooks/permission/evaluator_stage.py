"""The 6-mode permission evaluator, as a pipeline stage."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from igni_core.core.approvals.declined import declined_note
from igni_core.core.config.permission_eval import PermissionDecision, PermissionEvaluator
from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.hook_firer import HookFirer
from igni_core.core.hooks.permission.context import ToolCallContext
from igni_core.core.hooks.permission.denial import Denial
from igni_core.core.hooks.permission.outcome import Block, Continue, StageOutcome
from igni_core.core.hooks.permission.stage import PermissionStage
from igni_core.core.hooks.tool_events import PermissionDeniedPayload, PermissionRequestPayload

logger = logging.getLogger(__name__)

#: ``(tool_name, args, reason, agent_path) -> True / False / None`` — the
#: session's in-run approval channel; ``None`` means nobody can answer.
AskPerson = Callable[[str, dict[str, Any], str, list[str]], Awaitable[bool | None]]


class PermissionEvaluatorStage(PermissionStage):
    """The evaluator's decision for the call; a hook's ``allow`` skips it.

    DENY blocks with the evaluator's reason and fires ``PermissionDenied``.
    ASK fires ``PermissionRequest``; it has already been answered when the
    tool was built with Agno's approval pause, and is put to the person
    now when it was not. ALLOW and DEFER pass. Modes that never pause do
    not produce ASK — ``dontAsk`` resolves it to DENY in the evaluator.
    """

    skipped_by_hook_allow = True

    def __init__(
        self,
        evaluator: PermissionEvaluator | None,
        firer: HookFirer,
        ask_person: AskPerson | None = None,
    ) -> None:
        self._evaluator = evaluator
        self._firer = firer
        self._ask_person = ask_person

    async def evaluate(self, ctx: ToolCallContext) -> StageOutcome:
        if self._evaluator is None:
            return Continue()
        outcome = self._evaluator.evaluate_outcome(ctx.name, ctx.args)
        denial = Denial(ctx.name, outcome.reason)
        if outcome.decision is PermissionDecision.DENY:
            await self._fire_denied(ctx)
            logger.info("Permission DENY for %s: %s", ctx.name, outcome.reason)
            return Block(
                denial.policy_message(tool_is_off_entirely=self._tool_is_off_entirely(denial))
            )
        if outcome.decision is PermissionDecision.ASK:
            await self._firer.fire(
                HookEvent.PERMISSION_REQUEST,
                ctx.name,
                PermissionRequestPayload.from_call(ctx.name, ctx.args),
            )
            if ctx.built_with_pause() is False:
                # Built without a pause (a bypass start, or an ``allow``
                # category) in a mode that now asks: nobody has answered.
                return await self._ask_now(ctx, denial)
            logger.info(
                "Permission ASK for %s — falling through to HITL (already resolved by Agno)",
                ctx.name,
            )
        return Continue()

    async def _ask_now(self, ctx: ToolCallContext, denial: Denial) -> StageOutcome:
        """Put a call that skipped its pause to the person, or refuse it.

        With nobody to answer — a headless run, or no channel wired — the
        call is refused with a message that tells the model how to proceed.
        """
        answer: bool | None = None
        if self._ask_person is not None:
            try:
                answer = await self._ask_person(ctx.name, ctx.args, denial.reason, ctx.agent_path)
            except Exception as exc:  # fail closed
                logger.warning("asking about %s failed: %s — refusing", ctx.name, exc)
                answer = None
        if answer is True:
            return Continue()
        await self._fire_denied(ctx)
        if answer is False:
            return Block(declined_note(ctx.name))
        return Block(denial.unasked_message())

    def _tool_is_off_entirely(self, denial: Denial) -> bool:
        """Is the tool denied for every argument list, or just this one?

        Re-evaluated with no arguments: a bare ``deny`` rule and
        ``dontAsk`` still deny, while a scoped ``Bash(rm *)`` no longer
        matches and plan mode allows an argument-free shell call. Only
        asked for tools whose denial carries alternatives.
        """
        if self._evaluator is None or not denial.has_alternatives:
            return False
        return self._evaluator.evaluate(denial.tool_name, {}) is PermissionDecision.DENY

    async def _fire_denied(self, ctx: ToolCallContext) -> None:
        await self._firer.fire(
            HookEvent.PERMISSION_DENIED,
            ctx.name,
            PermissionDeniedPayload.from_call(ctx.name, ctx.args, "permission_evaluator"),
        )
