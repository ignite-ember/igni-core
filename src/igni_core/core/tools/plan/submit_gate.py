"""The ``PlanSubmit`` gate — may this plan go to the person?

Two audiences answer the question, in this order:

* the **extensions** that hook ``PlanSubmit`` — asked first, in the
  super admin's priority order, each handed what the one before left;
* igni's own **configured hooks** — they run on what the extensions
  left, so the project keeps the last word.

Either may refuse. A refusal carries its reason back to the agent,
which is told what to do about it and may submit again.

The gate owns that question and nothing else: it turns "may this
plan go ahead?" into a :class:`PlanSubmitDecision`. What the plan does
with the answer — the stores it writes, the checklist it fills — is
the coordinator's business
(:class:`~igni_core.core.tools.plan.submission.PlanTransactionCoordinator`).
"""

from __future__ import annotations

from pydantic import JsonValue

from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.hooks.extension_gate import ExtensionHookGate
from igni_core.core.hooks.extension_values import PlanSubmitValue
from igni_core.core.tools.plan.schemas import PlanSubmitDecision, PlanSubmitHookPayload


class PlanSubmitGate:
    """Asks the ``PlanSubmit`` question and answers it with a decision."""

    def __init__(self, extensions: ExtensionHookGate) -> None:
        """The extensions' half of the gate.

        The process's one :data:`~igni_core.core.hooks.extension_gate.extension_hook_gate`
        is injected, so the gate holds no global state of its own and
        a test can answer the question without an extension host.
        """
        self._extensions = extensions

    async def ask(
        self,
        *,
        session_id: str,
        plan: str,
        tasks: list[JsonValue],
        attempt: int,
        hooks: HookExecutor,
    ) -> PlanSubmitDecision:
        """Ask the extensions, then igni's own hooks.

        ``tasks`` are the checklist rows the agent suggested, as they
        will be committed — the extensions see them, and (by the
        ``PlanSubmit`` spec) cannot change them.

        ``hooks`` is the session's hook executor, handed in per call
        because a session re-mirrors its executor when the hooks are
        reloaded.

        Returns the decision: the plan as it may go ahead, or the
        refusal and its reason.
        """
        verdict = await self._extensions.ask(
            PlanSubmitValue(
                session_id=session_id,
                plan=plan,
                tasks=list(tasks),
                attempt=attempt,
            )
        )
        if not verdict.allowed:
            return PlanSubmitDecision(allowed=False, reason=verdict.reason)

        # An extension may have changed the plan; an empty answer keeps
        # the plan the agent wrote.
        decided_plan = verdict.value.plan.strip() or plan

        result = await hooks.execute(
            event=HookEvent.PLAN_SUBMIT.value,
            payload=PlanSubmitHookPayload(
                session_id=session_id,
                plan=decided_plan,
                attempt=attempt,
            ),
        )
        if not result.should_continue:
            return PlanSubmitDecision(
                allowed=False,
                reason=result.message or "a PlanSubmit hook rejected it",
            )
        return PlanSubmitDecision(allowed=True, plan=decided_plan)


__all__ = ["PlanSubmitGate"]
