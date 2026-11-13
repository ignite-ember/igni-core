"""Approving or dismissing a plan: where the session goes, and what a failed write leaves.

``PlanCoordinator`` records the person's decision, keeps it, and only then
leaves plan mode. Pinned here on a real ``PlanStore``:

* an approval returns to the mode the session started in, and to default
  when it started in plan mode;
* the switch goes through the mode coordinator, so an organisation's floor
  that keeps the session in plan mode is reported in ``mode_status``;
* a write that failed on approval puts the earlier decision back (or none)
  and says why, with the reason ``persist failed`` when the store gave none;
* a dismissal never changes the mode and stays decided even when the write
  failed;
* the researcher nudge is armed, read, and spent by the first consumer.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from igni_core.core.config.permission_eval import PermissionMode
from igni_core.core.session.broadcast import BroadcastBus
from igni_core.core.session.plan_ops import PlanCoordinator, PlanDeps
from igni_core.core.session.schemas import PermissionModeSwitch, PersistResult
from igni_core.core.tools.plan import PlanDecision
from igni_core.core.tools.plan.store import PlanStore


class Harness:
    def __init__(
        self,
        *,
        starting: PermissionMode = PermissionMode.DEFAULT,
        persist: PersistResult | None = None,
        switch: PermissionModeSwitch | None = None,
    ) -> None:
        self.store = PlanStore()
        self.switched_to: list[str] = []
        self.events: list[tuple[str, dict]] = []
        bus = BroadcastBus()
        bus.register(lambda e: self.events.append((e.channel, e.to_payload())))
        reply = switch or PermissionModeSwitch(changed=True, text="now in a mode")

        def set_mode(mode: str) -> PermissionModeSwitch:
            self.switched_to.append(mode)
            return reply

        self.coordinator = PlanCoordinator(
            PlanDeps(
                plan_store=self.store,
                persistence=SimpleNamespace(  # type: ignore[arg-type]
                    save_plan_decisions=AsyncMock(return_value=persist or PersistResult(ok=True))
                ),
                bus=bus,
                set_permission_mode=set_mode,
                starting_mode=lambda: starting,
            )
        )


@pytest.mark.parametrize(
    ("starting", "returns_to"),
    [
        (PermissionMode.DEFAULT, "default"),
        (PermissionMode.ACCEPT_EDITS, "acceptEdits"),
        (PermissionMode.PLAN, "default"),
    ],
)
async def test_an_approval_returns_to_the_starting_mode_or_default(starting, returns_to) -> None:
    h = Harness(starting=starting)

    result = await h.coordinator.approve("r1")

    assert result.ok
    assert h.switched_to == [returns_to]
    assert result.mode_status == "now in a mode"
    assert h.store.get_decision("r1") == "approved"


async def test_an_organisation_floor_that_keeps_plan_mode_is_reported_to_the_person() -> None:
    refusal = "Your organisation requires plan mode."
    h = Harness(switch=PermissionModeSwitch(changed=False, text=refusal))

    result = await h.coordinator.approve("r1")

    assert result.ok
    assert result.mode_status == refusal


async def test_a_failed_approval_write_restores_the_earlier_decision() -> None:
    h = Harness(persist=PersistResult(ok=False, error="disk full"))
    h.store.set_decision("r1", PlanDecision.DISMISSED)
    events_before = list(h.events)

    result = await h.coordinator.approve("r1")

    assert not result.ok
    assert result.error == "persist failed, mode flip aborted: disk full"
    assert h.store.get_decision("r1") == "dismissed", "the earlier decision stands"
    assert h.switched_to == []
    assert h.events == events_before, "nobody is told of a decision that was not kept"


async def test_a_failed_write_with_no_reason_says_persist_failed() -> None:
    h = Harness(persist=PersistResult(ok=False, error=""))

    result = await h.coordinator.approve("r1")

    assert result.error == "persist failed, mode flip aborted: persist failed"
    assert h.store.get_decision("r1") is None


async def test_a_dismissal_leaves_the_mode_and_stays_decided_when_the_write_fails() -> None:
    h = Harness(persist=PersistResult(ok=False, error="disk full"))

    result = await h.coordinator.dismiss("r1")

    assert result.ok
    assert result.mode_status == ""
    assert h.switched_to == []
    assert h.store.get_decision("r1") == "dismissed"
    assert h.events == [("plan_decided", {"run_id": "r1", "decision": "dismissed"})]


async def test_a_run_can_be_decided_again() -> None:
    h = Harness()

    await h.coordinator.dismiss("r1")
    await h.coordinator.approve("r1")

    assert h.store.get_decision("r1") == "approved"
    assert [payload["decision"] for _, payload in h.events] == ["dismissed", "approved"]


async def test_nothing_is_decided_for_a_session_without_a_plan_store() -> None:
    h = Harness()
    coordinator = PlanCoordinator(
        PlanDeps(
            plan_store=None,
            persistence=h.coordinator._deps.persistence,
            bus=BroadcastBus(),
            set_permission_mode=h.coordinator._deps.set_permission_mode,
            starting_mode=lambda: PermissionMode.DEFAULT,
        )
    )

    result = await coordinator.approve("r1")

    assert not result.ok
    assert "plan_store" in (result.error or "")
    assert h.switched_to == []


def test_the_researcher_nudge_is_spent_by_the_first_consumer() -> None:
    coordinator = Harness().coordinator
    assert coordinator.research_armed is False
    assert coordinator.consume_research_flag() is False

    coordinator.arm_research(True)
    assert coordinator.research_armed is True
    assert coordinator.consume_research_flag() is True
    assert coordinator.research_armed is False
    assert coordinator.consume_research_flag() is False

    coordinator.arm_research(True)
    coordinator.arm_research(False)
    assert coordinator.consume_research_flag() is False
