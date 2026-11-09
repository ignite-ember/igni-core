"""Settling the pauses the permission policy has already decided.

Agno pauses every call to a tool built with ``requires_confirmation``
— the shell, whenever ``shell_execute`` is ``ask`` — before the
permission evaluator has looked at it. The backend the windowed
clients use answers such a pause from the evaluator first
(``backend/pause_handler.py``): plan mode's read-only commands, an
allow rule, ``--accept-edits`` run, and a deny rule is refused,
without a dialog. The headless path did not, so a headless
``igni -m`` with ``permissions.mode: plan`` stopped at
``cat README.md`` — the very read docs/SECURITY.md says plan mode
lets through — and an allow rule was honoured in the window and not on
the command line.

The evaluator's verdict is applied exactly as the backend applies
it: ALLOW confirms, DENY rejects with its reason (the model reads
it), anything else is left paused and the turn ends as a pause.
Nothing is allowed here that the evaluator did not allow.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from agno.run.requirement import RunRequirement

from igni_core.core.config.permission_eval import (
    PermissionDecision,
    PermissionEvaluator,
    PermissionOutcome,
)
from igni_core.core.session.message_outcome import OutcomeKind, TurnOutcome
from igni_core.core.session.message_team import AnyRunOutput, MessageTeam

logger = logging.getLogger(__name__)


class PausePolicyResolver:
    """Answers the pauses the permission policy has already decided.

    The evaluator is read through a callable because the session
    rebuilds it with the team.
    """

    #: How many times one turn may stop at a pause the policy settles
    #: before the resolver stops resuming it. A turn that reads a dozen
    #: files pauses a dozen times; a run that loops is cut off rather
    #: than spun forever.
    MAX_POLICY_RESUMES = 50

    def __init__(
        self,
        *,
        evaluator_ref: Callable[[], PermissionEvaluator | None],
        session_id_ref: Callable[[], str],
    ) -> None:
        self._evaluator_ref = evaluator_ref
        self._session_id_ref = session_id_ref

    async def settle(self, team: MessageTeam, run: AnyRunOutput) -> AnyRunOutput:
        """Confirm or reject what the policy decided, and continue the run.

        A pause the policy leaves undecided is left paused: the turn
        ends there, and the caller reports it as the failure it is.
        """
        evaluator = self._evaluator_ref()
        if evaluator is None:
            return run

        for _ in range(self.MAX_POLICY_RESUMES):
            outcome = TurnOutcome.from_run(run)
            if outcome.kind is not OutcomeKind.PAUSED:
                return run
            if not outcome.requirements:
                return run
            verdicts = self._verdicts(evaluator, outcome.requirements)
            if verdicts is None:
                return run
            for requirement, decision in verdicts:
                if decision.decision is PermissionDecision.ALLOW:
                    requirement.confirm()
                else:
                    requirement.reject(note=f"Blocked: {decision.reason}")
            run = await team.acontinue_run(
                run_response=run,
                requirements=list(run.requirements or []),
                session_id=self._session_id_ref(),
                stream=False,
            )
        return run

    def _verdicts(
        self,
        evaluator: PermissionEvaluator,
        requirements: list[RunRequirement],
    ) -> list[tuple[RunRequirement, PermissionOutcome]] | None:
        """What the policy decides about each requirement.

        ``None`` when the policy cannot answer for every one of them:
        a pause is settled whole or not at all, so one undecided call
        leaves the whole pause alone.
        """
        verdicts: list[tuple[RunRequirement, PermissionOutcome]] = []
        for requirement in requirements:
            execution = requirement.tool_execution
            name = str(execution.tool_name or "") if execution is not None else ""
            args = dict(execution.tool_args or {}) if execution is not None else {}
            try:
                decision = evaluator.evaluate_outcome(name, args)
            except Exception:  # noqa: BLE001 — an unanswerable pause is reported, not guessed
                logger.warning("permission evaluation failed for %s", name, exc_info=True)
                return None
            if decision.decision not in (PermissionDecision.ALLOW, PermissionDecision.DENY):
                return None
            verdicts.append((requirement, decision))
        return verdicts


__all__ = ["PausePolicyResolver"]
