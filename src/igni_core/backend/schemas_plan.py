"""Typed wire schemas for plan mode + todo panel.

Extracted from :mod:`igni_core.backend.server` — the
``LatestPlanResult`` model previously lived inline in the god-class
file. Sibling schemas modules (see ``schemas_run.py`` /
``schemas_pause.py``) are the pattern for backend wire types; this
file follows that convention.

The FE reads :class:`LatestPlanResult` on plan-panel open and after
every ``exit_plan_mode`` submission.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from igni_core.core.tools.todo import TodoItemWire

#: Display state of the plan-mode panel. ``""`` = no plan submitted;
#: ``"pending"`` = plan exists but the user hasn't clicked
#: approve/dismiss. Shared with :class:`schemas_history.PlanTurn` so
#: the plan-panel snapshot and the rebuilt-history turn speak one
#: vocabulary (single source of truth for the FE contract).
PlanState = Literal["pending", "approved", "dismissed", ""]


class LatestPlanResult(BaseModel):
    """Wire shape for :meth:`PlanSnapshotBuilder.latest` — the
    plan-mode panel reads this on open + after each ``exit_plan_mode``.

    ``state`` is ``"pending"`` when a plan exists (user hasn't
    approved/dismissed yet) or ``""`` when no plan submitted.
    ``tasks`` mirrors :attr:`TodoStore.items` (each row dumps
    ``activeForm`` camelCase on its own, see :class:`TodoItemWire`) so the FE renders the plan and task list from a
    single payload."""

    latest: str = ""
    history: list[str] = Field(default_factory=list)
    tasks: list[TodoItemWire] = Field(default_factory=list)
    state: PlanState = ""
