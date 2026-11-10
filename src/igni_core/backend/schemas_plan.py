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


# Built by PlanSnapshotBuilder.latest; ``tasks`` mirrors TodoStore.items
# (each row dumps ``activeForm`` camelCase on its own, see TodoItemWire).
class LatestPlanResult(BaseModel):
    """The session's current plan-mode plan and task list.

    Read it when opening a plan view and again after the agent submits
    a plan."""

    latest: str = Field(
        default="", description="The most recent plan text (Markdown); empty if none."
    )
    history: list[str] = Field(
        default_factory=list, description="Earlier plans submitted in this session, oldest first."
    )
    tasks: list[TodoItemWire] = Field(
        default_factory=list, description="The session's current task (todo) list."
    )
    state: PlanState = Field(
        default="",
        description=(
            "`pending` when a plan is waiting for the user to approve or "
            'dismiss it; `""` when no plan has been submitted.'
        ),
    )
