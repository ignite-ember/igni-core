"""Plan-mode decision recording — coordinator class only.

Owns the persist-then-mode-flip flow for a user's approve /
dismiss decision on a plan the agent submitted via
``exit_plan_mode``. Session composes one
:class:`PlanCoordinator` instance, handed a :class:`PlanDeps`.

Side-effect ordering matters and is now an ENFORCED contract
rather than a "log-and-hope": the persistence write happens
BEFORE the mode flip so a crash mid-flip doesn't leave the user
with ``mode=default`` and no recorded approval — the original
bug that motivated persisting these decisions in the first
place. If the DB write fails on the approve path, the mode
flip is ABORTED and the coordinator returns
:class:`PlanDecisionResult` with ``ok=False``. This is a
semantic tightening of the pre-refactor "swallow-and-flip"
behaviour and matches the top-of-file invariant.

The wire shapes — :class:`PlanDecisionResult` (return envelope)
and :class:`PlanDecidedEvent` (broadcast event) — live in
:mod:`~igni_core.core.session.schemas` alongside sibling
coordinator wire types (:class:`CompactResult`,
:class:`LoopAdvance`). The re-export at the bottom of this
module keeps ``from igni_core.core.session.plan_ops import
PlanDecisionResult`` working for legacy callers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from igni_core.core.config.permission_eval import PermissionMode
from igni_core.core.session.broadcast import BroadcastBus
from igni_core.core.session.persistence import SessionPersistence
from igni_core.core.session.schemas import (
    PermissionModeSwitch,
    PlanDecidedEvent,
    PlanDecisionResult,
)
from igni_core.core.tools.plan import PlanDecision, PlanStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlanDeps:
    """What recording a plan decision touches.

    * ``plan_store`` / ``persistence`` — where the decision is kept and
      written; ``None`` makes a decision fail with a reason instead.
    * ``bus`` — where :class:`PlanDecidedEvent` goes.
    * ``set_permission_mode`` / ``starting_mode`` — the mode
      coordinator's switch (which enforces the organisation's floor)
      and where an approval returns to.
    """

    plan_store: PlanStore | None
    persistence: SessionPersistence | None
    bus: BroadcastBus
    set_permission_mode: Callable[[str], PermissionModeSwitch]
    starting_mode: Callable[[], PermissionMode]


class PlanCoordinator:
    """Owns the persist-then-mode-flip flow for plan decisions."""

    def __init__(self, deps: PlanDeps) -> None:
        self._deps = deps
        self._research_armed = False

    @property
    def research_armed(self) -> bool:
        return self._research_armed

    def arm_research(self, armed: bool) -> None:
        """Arm or disarm the plan-mode researcher nudge for the next turn."""
        self._research_armed = armed

    def consume_research_flag(self) -> bool:
        """Whether the nudge is armed, disarming it."""
        armed, self._research_armed = self._research_armed, False
        return armed

    async def approve(self, run_id: str) -> PlanDecisionResult:
        """Record the user's approval and flip out of plan mode."""
        return await self._record(run_id, PlanDecision.APPROVED, flip_mode=True)

    async def dismiss(self, run_id: str) -> PlanDecisionResult:
        """Record the user's dismissal (they want to iterate on the
        plan, so we do NOT flip the mode)."""
        return await self._record(run_id, PlanDecision.DISMISSED, flip_mode=False)

    async def _record(
        self,
        run_id: str,
        decision: PlanDecision,
        *,
        flip_mode: bool,
    ) -> PlanDecisionResult:
        """Common body for :meth:`approve` / :meth:`dismiss`.

        Returns a :class:`PlanDecisionResult` envelope on every
        path — including validation refusals and DB failures on
        the flip path. Callers key on ``result.ok`` to decide
        whether to surface a success confirmation or an inline
        error to the FE.
        """
        if not run_id:
            return PlanDecisionResult(
                run_id=run_id,
                decision=decision,
                ok=False,
                error="run_id must be non-empty",
            )
        deps = self._deps
        store = deps.plan_store
        if store is None:
            return PlanDecisionResult(
                run_id=run_id,
                decision=decision,
                ok=False,
                error="plan_store not initialised on this session",
            )
        store.set_decision(run_id, decision)
        # Persist BEFORE flipping mode so a crash mid-flip doesn't
        # leave the user with mode=default but no recorded approval —
        # the original bug that motivated persisting these decisions.
        # ``decisions_snapshot`` returns a typed :class:`PlanDecisionsBlob`
        # which persistence dumps at its own boundary — the caller-side
        # dict comprehension is gone (Rule 1).
        persistence = deps.persistence
        if persistence is None:
            return PlanDecisionResult(
                run_id=run_id,
                decision=decision,
                ok=False,
                error="persistence not initialised on this session",
            )
        blob = store.decisions_snapshot()
        try:
            await persistence.save_plan_decisions(blob)
        except Exception as exc:
            # On the approve path, the persist-before-flip invariant
            # is load-bearing: if we flipped the mode without a
            # recorded approval, a restart would surface the original
            # "mode=default with pending plan" bug. Abort the flip
            # and surface the error via the Pattern-3 envelope.
            if flip_mode:
                logger.warning(
                    "plan decision persist failed on approve path — "
                    "mode flip aborted to preserve invariant: %s",
                    exc,
                )
                return PlanDecisionResult(
                    run_id=run_id,
                    decision=decision,
                    ok=False,
                    error=f"persist failed, mode flip aborted: {exc}",
                )
            # Dismiss path — no mode flip to guard, best-effort
            # persistence loss is acceptable (the next decision
            # rewrites the blob anyway).
            logger.debug("plan decision persist failed: %s", exc)
        mode_status = ""
        if flip_mode:
            # Out of plan mode to the mode the session started in, or
            # ``default`` when it started in plan mode. Goes through
            # ``set_permission_mode`` like every other switch, so an
            # organisation's floor still holds: a plan-mode floor keeps
            # the session in plan mode and ``mode_status`` says why.
            start = deps.starting_mode()
            if start is PermissionMode.PLAN:
                start = PermissionMode.DEFAULT
            mode_status = deps.set_permission_mode(start.value).text
        deps.bus.emit(PlanDecidedEvent(run_id=run_id, decision=decision))
        return PlanDecisionResult(
            run_id=run_id,
            decision=decision,
            mode_status=mode_status,
        )


# ── Legacy re-export ─────────────────────────────────────────
#
# :class:`PlanDecisionResult` now lives in
# :mod:`~igni_core.core.session.schemas` alongside sibling
# coordinator wire types. The re-export below keeps existing
# ``from igni_core.core.session.plan_ops import
# PlanDecisionResult`` imports working.

__all__ = ["PlanCoordinator", "PlanDecisionResult", "PlanDeps"]
