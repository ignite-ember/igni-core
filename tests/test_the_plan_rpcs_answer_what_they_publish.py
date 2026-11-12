"""The plan RPCs, called by their wire arguments, over a real plan coordinator.

``approve_plan`` and ``dismiss_plan`` reach a real :class:`PlanCoordinator`
over a real :class:`PlanStore`, so the answer is what a window would get:
the run, the decision, the mode line and ``ok`` — and the ``plan_decided``
event the bus carries. ``get_latest_plan`` reads the same store through
the real snapshot builder.

Pinned:

* ``run_id`` is optional on the wire and empty is refused in the answer
  (``ok: false``), not as invalid arguments; a number is its string; an
  unknown key is refused as an argument;
* approving records the decision, returns to the starting mode and
  emits ``plan_decided``; dismissing records and stays in plan mode;
* a decision that cannot be recorded answers ``ok: false``;
* ``get_latest_plan`` says ``pending`` while a plan awaits a decision;
* each decision declares its pushes and correlates by ``run_id``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from igni_core.backend.plan_snapshot_builder import PlanSnapshotBuilder
from igni_core.backend.rpc_binding import Correlation, RpcArgsError, RpcBinding
from igni_core.backend.rpc_handlers.plan import PlanRpcHandler
from igni_core.core.config.permission_eval import PermissionMode
from igni_core.core.session.broadcast import BroadcastBus
from igni_core.core.session.plan_ops import PlanCoordinator, PlanDeps
from igni_core.core.session.schemas import PermissionModeSwitch, PersistResult
from igni_core.core.tools.plan import PlanDecision, PlanStore
from igni_core.core.tools.todo import TodoStore
from igni_core.protocol.rpc import RpcMethod, RpcScope
from igni_core.protocol.schemas.enums import PushChannel


class _Persistence:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok

    async def save_plan_decisions(self, blob: Any) -> PersistResult:
        return PersistResult(ok=self.ok, error="" if self.ok else "disk full")


class _Rig:
    def __init__(self, *, persist_ok: bool = True) -> None:
        self.store = PlanStore()
        self.bus = BroadcastBus()
        self.events: list[tuple[str, dict]] = []
        self.bus.register(lambda e: self.events.append((e.channel, e.to_payload())))
        self.modes: list[str] = []

        def switch(mode: str) -> PermissionModeSwitch:
            self.modes.append(mode)
            return PermissionModeSwitch(changed=True, text=f"mode → {mode}")

        plan = PlanCoordinator(
            PlanDeps(
                plan_store=self.store,
                persistence=_Persistence(persist_ok),  # type: ignore[arg-type]
                bus=self.bus,
                set_permission_mode=switch,
                starting_mode=lambda: PermissionMode.ACCEPT_EDITS,
            )
        )
        session = MagicMock()
        session.plan_store = self.store
        session.todo_store = TodoStore()
        backend = SimpleNamespace(
            session=SimpleNamespace(plan=plan),
            get_latest_plan=PlanSnapshotBuilder(session).latest,
        )
        self.calls = PlanRpcHandler(SimpleNamespace(backend=backend)).methods()  # type: ignore[arg-type]


async def test_approving_records_the_decision_returns_to_the_starting_mode_and_announces_it():
    rig = _Rig()

    answer = await rig.calls[RpcMethod.APPROVE_PLAN]({"run_id": "r-1"})

    assert (answer.run_id, answer.decision, answer.ok) == ("r-1", PlanDecision.APPROVED, True)
    assert answer.mode_status == f"mode → {PermissionMode.ACCEPT_EDITS.value}"
    assert rig.modes == [PermissionMode.ACCEPT_EDITS.value]
    assert rig.store.decisions["r-1"] == PlanDecision.APPROVED
    assert rig.events == [("plan_decided", {"run_id": "r-1", "decision": "approved"})]


async def test_dismissing_records_the_decision_and_stays_in_plan_mode():
    rig = _Rig()

    answer = await rig.calls[RpcMethod.DISMISS_PLAN]({"run_id": "r-2"})

    assert (answer.run_id, answer.decision, answer.ok) == ("r-2", PlanDecision.DISMISSED, True)
    assert answer.mode_status == ""
    assert rig.modes == []
    assert rig.store.decisions["r-2"] == PlanDecision.DISMISSED
    assert rig.events == [("plan_decided", {"run_id": "r-2", "decision": "dismissed"})]


@pytest.mark.parametrize("method", [RpcMethod.APPROVE_PLAN, RpcMethod.DISMISS_PLAN])
@pytest.mark.parametrize("wire", [{}, {"run_id": ""}])
async def test_a_decision_without_a_run_id_is_refused_in_the_answer(method, wire):
    rig = _Rig()

    answer = await rig.calls[method](wire)

    assert answer.ok is False
    assert answer.error == "run_id must be non-empty"
    assert rig.store.decisions == {} and rig.modes == [] and rig.events == []


@pytest.mark.parametrize("method", [RpcMethod.APPROVE_PLAN, RpcMethod.DISMISS_PLAN])
async def test_a_numeric_run_id_is_taken_as_its_string(method):
    rig = _Rig()

    answer = await rig.calls[method]({"run_id": 42})

    assert answer.run_id == "42"
    assert "42" in rig.store.decisions


@pytest.mark.parametrize("method", [RpcMethod.APPROVE_PLAN, RpcMethod.DISMISS_PLAN])
def test_a_key_the_decision_does_not_name_is_refused_as_an_argument(method):
    rig = _Rig()

    with pytest.raises(RpcArgsError, match="'runId' is not an argument of this call"):
        rig.calls[method]({"runId": "r-1"})


async def test_an_approval_that_cannot_be_recorded_leaves_the_mode_and_the_plan_alone():
    rig = _Rig(persist_ok=False)

    answer = await rig.calls[RpcMethod.APPROVE_PLAN]({"run_id": "r-3"})

    assert answer.ok is False
    assert "disk full" in answer.error
    assert rig.modes == [] and rig.events == []
    assert "r-3" not in rig.store.decisions


async def test_the_latest_plan_is_pending_until_decided_and_keeps_the_earlier_ones():
    rig = _Rig()
    rig.store.set_plan("first plan")
    rig.store.set_plan("second plan")

    pending = rig.calls[RpcMethod.GET_LATEST_PLAN]({})

    assert (pending.latest, pending.history, pending.state) == (
        "second plan",
        ["first plan"],
        "pending",
    )
    with pytest.raises(RpcArgsError, match="'x' is not an argument"):
        rig.calls[RpcMethod.GET_LATEST_PLAN]({"x": 1})


def test_no_plan_submitted_is_an_empty_answer():
    answer = _Rig().calls[RpcMethod.GET_LATEST_PLAN]({})

    assert (answer.latest, answer.history, answer.tasks, answer.state) == ("", [], [], "")


def test_the_decisions_declare_their_pushes_and_correlate_by_run_id():
    bindings = RpcBinding.on(PlanRpcHandler)
    approve = bindings[RpcMethod.APPROVE_PLAN][1]
    dismiss = bindings[RpcMethod.DISMISS_PLAN][1]

    for binding in (approve, dismiss):
        assert binding.correlate == Correlation(by="payload.run_id", source="args.run_id")
        assert binding.scope is RpcScope.SESSION
    assert approve.streams == (PushChannel.PERMISSION_MODE_CHANGED, PushChannel.PLAN_DECIDED)
    assert dismiss.streams == (PushChannel.PLAN_DECIDED,)
    assert bindings[RpcMethod.GET_LATEST_PLAN][1].streams == ()
