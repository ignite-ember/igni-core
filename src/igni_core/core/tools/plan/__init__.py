"""Plan mode — Claude Code parity (row 50).

A user-toggled mode where the agent can read / search / think but
**cannot** write files or run mutating shell commands. The
enforcement lives in :class:`PermissionEvaluator` (already
implemented as ``PermissionMode.PLAN`` with row 7); this package
adds the missing pieces:

* :class:`PlanStore` — per-session capture of the latest plan
  the agent submitted via ``exit_plan_mode``. Surfaced to the UI
  via the ``GET_LATEST_PLAN`` RPC.
* :class:`PlanTool` — registers ``exit_plan_mode(plan)`` +
  ``enter_plan_mode(reason, task)`` so the agent can toggle
  into plan mode and signal "I'm done planning" at the end of a
  plan-mode turn. The tool does NOT flip the mode out of plan
  automatically — the user controls that via ``/plan`` so the
  agent can't exit the sandbox on its own.
* :class:`PlanResearcherRunner` — spawns the
  ``plan_researcher`` sub-agent via :class:`OrchestrateTools`
  when :meth:`PlanTool.enter_plan_mode` is called with
  ``task=...``.
* :class:`PlanTransactionCoordinator` — the two plan transactions
  (enter / submit), which :class:`PlanTool` delegates to.
* :class:`PlanSubmitGate` — may this plan go to the person? The
  extensions answer first, then igni's own ``PlanSubmit`` hooks.

The complementary half (``/plan`` slash command +
``RuntimeModeCoordinator.set_permission_mode``) lives in ``backend/command_handler.py``
and ``core/session/core.py`` respectively.

This module was a single 517-LoC file until the OOP split; the
symbols it exported are re-exported here.
"""

from igni_core.core.tools.plan.researcher import PlanResearcherRunner
from igni_core.core.tools.plan.schemas import (
    PlanEnterResult,
    PlanExitResult,
    PlanModeEnteredEvent,
    PlanSubmitDecision,
    PlanSubmittedEvent,
    PlanTaskBatch,
    PlanTaskInput,
)
from igni_core.core.tools.plan.store import (
    PlanDecision,
    PlanDecisionsBlob,
    PlanSnapshot,
    PlanStore,
)
from igni_core.core.tools.plan.submission import PlanTransactionCoordinator
from igni_core.core.tools.plan.submit_gate import PlanSubmitGate
from igni_core.core.tools.plan.tool import PlanTool

__all__ = [
    "PlanDecision",
    "PlanDecisionsBlob",
    "PlanEnterResult",
    "PlanExitResult",
    "PlanModeEnteredEvent",
    "PlanResearcherRunner",
    "PlanSnapshot",
    "PlanStore",
    "PlanSubmitDecision",
    "PlanSubmitGate",
    "PlanSubmittedEvent",
    "PlanTaskBatch",
    "PlanTaskInput",
    "PlanTool",
    "PlanTransactionCoordinator",
]
