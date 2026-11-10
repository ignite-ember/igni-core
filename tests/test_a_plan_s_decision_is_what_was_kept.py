"""A plan's state is the decision made on it, and only a decision kept.

* ``get_latest_plan`` said ``pending`` for any plan, decided or not.
* ``approve_plan`` recorded the approval in memory before checking it
  could be kept; when the write failed it answered ``ok: false`` and the
  plan still read as approved.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from igni_core.core.config.permission_eval import PermissionMode
from igni_core.core.session.broadcast import BroadcastBus
from igni_core.core.session.plan_ops import PlanCoordinator, PlanDeps
from igni_core.core.session.schemas import PermissionModeSwitch, PersistResult
from igni_core.core.tools.plan import PlanDecision
from igni_core.core.tools.plan.store import PlanStore


def test_the_latest_plan_reads_as_the_decision_made_on_it() -> None:
    store = PlanStore()
    assert store.latest_state() == ""

    store.set_plan("step one")
    assert store.latest_state() == "pending"

    store.set_decision("r1", PlanDecision.APPROVED)
    assert store.latest_state() == "approved"

    store.set_plan("step two")
    assert store.latest_state() == "pending", "a new plan is undecided"

    store.set_decision("r2", PlanDecision.DISMISSED)
    assert store.latest_state() == "dismissed"


def test_a_plan_read_back_from_history_has_its_decision() -> None:
    store = PlanStore()
    store.set_plan("from history", run_id="r9")
    store.load_decisions(SimpleNamespace(decisions={"r9": PlanDecision.APPROVED}))  # type: ignore[arg-type]

    assert store.latest_state() == "approved"


def _coordinator(store: PlanStore, *, persisted: bool) -> PlanCoordinator:
    persistence = SimpleNamespace(
        save_plan_decisions=AsyncMock(
            return_value=PersistResult(ok=persisted, error="" if persisted else "disk full")
        )
    )
    return PlanCoordinator(
        PlanDeps(
            plan_store=store,
            persistence=persistence,  # type: ignore[arg-type]
            bus=BroadcastBus(),
            set_permission_mode=MagicMock(
                return_value=PermissionModeSwitch(changed=True, text="mode → default")
            ),
            starting_mode=lambda: PermissionMode.DEFAULT,
        )
    )


async def test_an_approval_that_could_not_be_kept_leaves_the_plan_pending() -> None:
    store = PlanStore()
    store.set_plan("the plan")

    result = await _coordinator(store, persisted=False).approve("r1")

    assert not result.ok
    assert store.get_decision("r1") is None
    assert store.latest_state() == "pending"


async def test_an_approval_that_was_kept_is_the_plan_s_state() -> None:
    store = PlanStore()
    store.set_plan("the plan")

    result = await _coordinator(store, persisted=True).approve("r1")

    assert result.ok
    assert store.latest_state() == "approved"
