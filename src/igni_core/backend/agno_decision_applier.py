"""Apply a person's HITL decision to an Agno requirement.

:class:`AgnoDecisionApplier` owns the ``req.confirm()`` /
``req.reject()`` dispatch behind :class:`HitlAction` (Pattern 7 — the
wire keeps the string literal; the domain uses the enum). It never
builds ``msg.*`` frames: it returns an :class:`ApplyDecisionResult`
and the caller (:meth:`HitlController.resolve_batch`) turns a failure
into the wire ``msg.Error``.
"""

from __future__ import annotations

import logging

from igni_core.backend.schemas_hitl import (
    ApplyDecisionResult,
    HitlAction,
    RequirementTool,
    RunRequirement,
)
from igni_core.core.approvals.declined import declined_note
from igni_core.protocol import messages as msg

logger = logging.getLogger(__name__)


class AgnoDecisionApplier:
    """Applies an :class:`msg.HITLDecision` to an Agno
    :class:`RunRequirement` via ``confirm()`` / ``reject()``.

    Stateless; one module-level instance in :mod:`hitl_controller`
    is shared by every controller.
    """

    def apply(self, decision: msg.HITLDecision, req: RunRequirement) -> ApplyDecisionResult:
        """Call ``req.confirm()`` or ``req.reject(...)``.

        Returns :class:`ApplyDecisionResult` — ``ok=False`` when the
        action is unknown or the Agno call raised.
        """
        try:
            action = HitlAction(decision.action)
        except ValueError:
            reason = (
                f"Unknown HITL action '{decision.action}' for requirement {decision.requirement_id}"
            )
            logger.warning("resolve_hitl_batch: %s", reason)
            return ApplyDecisionResult(ok=False, reason=reason)

        try:
            if action is HitlAction.CONFIRM:
                req.confirm()
            else:
                req.reject(note=declined_note(RequirementTool.of(req).name))
        except Exception as exc:
            logger.warning(
                "resolve_hitl_batch: requirement %s %s() raised %s; skipping",
                decision.requirement_id,
                action.value,
                exc,
            )
            return ApplyDecisionResult(
                ok=False,
                reason=f"Failed to {action.value} requirement {decision.requirement_id}: {exc}",
            )
        return ApplyDecisionResult(ok=True)


__all__ = ["AgnoDecisionApplier"]
