"""Plan-mode transaction coordinator — the two plan transactions.

:class:`PlanTool` is the agent-facing adapter; this module owns what
the adapter used to do inline:

* ``enter_plan_mode`` — flip the permission mode, broadcast the
  agent-attributed follow-up, and spawn the ``plan_researcher``
  sub-agent when the agent named a task.
* ``exit_plan_mode`` — ask the :class:`PlanSubmitGate` whether the
  plan may go to the person, write the plan, fill the todo
  checklist, and queue the ``plan_submitted`` broadcast.

Every collaborator is named and typed: the session's plan store, todo
store, broadcast bus, permission-mode switch, hook executor and id.
They are construction invariants of
:class:`~igni_core.core.session.core.Session`, so there is no "is the
session shaped right?" branch to take and no exception raised to
carry a refusal out of one.

Refusals are values, not exceptions: both transactions return a typed
envelope (:class:`PlanEnterResult`, :class:`PlanExitResult`) and the
tool renders ``result.reply_text`` — one render site each.

The coordinator does not reach past the session's public surface: it
calls the stores' own methods (``set_plan``, ``reset_rejections``,
``record_rejection``) instead of writing their fields, and it reads
the session's attributes directly rather than probing for them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from igni_core.core.tools.plan.researcher import PlanResearcherRunner
from igni_core.core.tools.plan.schemas import (
    PlanEnterResult,
    PlanExitResult,
    PlanModeEnteredEvent,
    PlanSubmittedEvent,
    PlanTaskBatch,
    PlanTaskInput,
)
from igni_core.core.tools.plan.submit_gate import PlanSubmitGate
from igni_core.core.tools.todo import TodoItemWire

if TYPE_CHECKING:
    from igni_core.core.session.core import Session


class PlanTransactionCoordinator:
    """Owns the plan-mode enter / submit transactions against a session.

    Public API:

    * :meth:`enter` — :class:`PlanEnterResult` for the
      ``enter_plan_mode`` transaction (flip the mode → broadcast the
      agent-attributed follow-up → optionally spawn the researcher).
    * :meth:`submit` — :class:`PlanExitResult` for the
      ``exit_plan_mode`` transaction (ask the gate → set the plan →
      populate the todos → queue the ``plan_submitted`` broadcast).
    """

    def __init__(self, session: Session, gate: PlanSubmitGate) -> None:
        self._session = session
        self._gate = gate
        self._researcher = PlanResearcherRunner(session)

    # ── enter_plan_mode transaction ────────────────────────────

    async def enter(self, reason: str, task: str) -> PlanEnterResult:
        """Run the enter-plan-mode transaction and return a typed
        envelope. See :class:`PlanEnterResult` for field semantics."""
        reason_clean = (reason or "").strip()
        task_clean = (task or "").strip()

        switch = self._session.mode.set_permission_mode("plan")
        if switch.refused:
            # Evaluator refused the flip — propagate the status
            # verbatim.
            return PlanEnterResult(
                ok=False,
                mode_status=switch.text,
                reason="evaluator_refused",
                error=switch.text,
                reply_text=switch.text,
            )

        # A fresh plan: PlanSubmit hooks see attempt 0.
        self._session.plan_store.reset_rejections()

        # Re-broadcast with the agent attribution + reason so
        # the FE can render a small info banner.
        self._session.broadcast_bus.emit(
            PlanModeEnteredEvent(mode="plan", source="agent", reason=reason_clean)
        )

        # Spawn the plan_researcher sub-agent on the task. The
        # researcher operates in plan mode itself (same evaluator
        # — file edits blocked) and produces a structured report
        # the caller turns into the final ``exit_plan_mode`` call.
        researcher_report = await self._researcher.run(task_clean) if task_clean else ""

        return PlanEnterResult(
            ok=True,
            mode_flipped=switch.changed,
            mode_status=switch.text,
            researcher_report=researcher_report,
            reply_text=self._render_enter_reply(
                reason_clean=reason_clean,
                researcher_report=researcher_report,
            ),
        )

    def _render_enter_reply(self, *, reason_clean: str, researcher_report: str) -> str:
        """Single render site for the ``enter_plan_mode`` reply
        text. Preserves the "Entered plan mode" substring the
        tests match on."""
        tail = f" ({reason_clean})" if reason_clean else ""
        header = f"Entered plan mode{tail}. File edits and mutating shell commands are now blocked."
        if researcher_report:
            return (
                f"{header}\n\nplan_researcher sub-agent report follows. "
                "Review it, refine if needed, then call "
                "exit_plan_mode(plan, tasks=[...]) with the final plan. "
                "If the report is thin, call enter_plan_mode again with "
                "a sharper task description for another research pass.\n\n"
                "---\n\n"
                f"{researcher_report}"
            )
        return (
            # `file_read` is a permission category, not a function: PRD-12's
            # fix reached the plan prompts and missed this tool result.
            f"{header} Gather context (your read-only tools, and "
            'run_shell_command "cat <path>"), '
            "then call exit_plan_mode(plan, tasks=[...]) with a concrete proposal. "
            "Tip: pass the user's request as `task=` next time and this tool will "
            "spawn the plan_researcher sub-agent for you automatically."
        )

    # ── exit_plan_mode transaction ─────────────────────────────

    async def submit(self, plan: str, tasks: list | None) -> PlanExitResult:
        """Run the submit-plan transaction and return a typed
        envelope. See :class:`PlanExitResult` for field semantics.

        ``tasks`` arrives as the agent's loose JSON rows; they become
        :class:`PlanTaskInput` models here, and the rows that could
        not be used come back as named errors instead of disappearing.
        """
        plan_text = (plan or "").strip()
        if not plan_text:
            msg = (
                "Error: plan is empty. "
                "Pass a markdown-formatted plan describing what you intend to do."
            )
            return PlanExitResult(
                ok=False,
                reason="empty_plan",
                error=msg,
                reply_text=msg,
            )

        store = self._session.plan_store
        batch = PlanTaskInput.validate_batch(tasks)

        decision = await self._gate.ask(
            session_id=self._session.session_id,
            plan=plan_text,
            tasks=[task.model_dump(by_alias=True) for task in batch.tasks],
            attempt=store.rejected,
            hooks=self._session.hook_executor,
        )
        if not decision.allowed:
            return self._rejected(decision.reason)

        store.set_plan(decision.plan)
        task_snapshot = self._fill_checklist(batch)

        # Broadcast so the FE can render the plan card inline +
        # show the approve/reject buttons. Deferred to AFTER the
        # run finishes: the PlanCard is the outcome of the run,
        # so it should land below the agent's closing reply in
        # the chat list, not mid-stream above it.
        self._session.broadcast_bus.queue_post_run(
            PlanSubmittedEvent(plan=decision.plan, tasks=list(task_snapshot))
        )

        return PlanExitResult(
            ok=True,
            task_snapshot=task_snapshot,
            validation_errors=batch.errors,
            reply_text=self._render_submit_reply(
                task_snapshot=task_snapshot,
                validation_errors=batch.errors,
            ),
        )

    def _fill_checklist(self, batch: PlanTaskBatch) -> list[TodoItemWire]:
        """Put the validated checklist into the todo store and return
        the store's snapshot.

        A plan without rows leaves the store alone and snapshots
        nothing — the checklist is what the agent enumerated.
        """
        if not batch.tasks:
            return []
        todo_store = self._session.todo_store
        todo_store.set([task.to_wire() for task in batch.tasks])
        return todo_store.items

    def _rejected(self, reason: str) -> PlanExitResult:
        """The gate refused: count the rejection and tell the agent
        what to do about it."""
        self._session.plan_store.record_rejection()
        reply = f"Plan rejected: {reason}"
        return PlanExitResult(ok=False, reason="rejected", error=reply, reply_text=reply)

    def _render_submit_reply(
        self,
        *,
        task_snapshot: list[TodoItemWire],
        validation_errors: list[str],
    ) -> str:
        """Single render site for the ``exit_plan_mode`` reply
        text. Preserves the "Plan submitted" / "Tasks validation
        errors" substrings the tests match on."""
        reply = (
            "Plan submitted. Stop here — the user will review and either "
            "exit plan mode via `/plan` (to let you execute) or ask for "
            "changes. Do not continue executing in this turn."
        )
        if task_snapshot:
            reply += (
                f" ({len(task_snapshot)} structured task(s) populated; "
                "they'll tick off as you call todo_write during execution)."
            )
        if validation_errors:
            reply += f" Tasks validation errors (ignored): {'; '.join(validation_errors)}"
        return reply


__all__ = [
    "PlanTransactionCoordinator",
]
