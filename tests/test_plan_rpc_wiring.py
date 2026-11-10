"""RPC dispatch wiring for ``approve_plan`` / ``dismiss_plan``.

The fixture-FE Playwright suite proves the FE puts the right
envelope on the wire. The unit suite proves ``Session.plan.approve``
does the right thing once called. This file pins the missing
middle: that the dispatch lambdas in ``RpcRouter.build_table`` route
the wire envelope to the right ``Session`` method with ``run_id``
extracted from ``args`` correctly.

The kind of bug this guards against:

* ``args.get("runId")`` (camelCase) when the FE sent
  ``run_id`` — silent ``""`` extraction → ``ValueError("run_id
  must be non-empty")`` from ``Session.plan.approve``, which the
  fixture-FE spec wouldn't catch because the fixture replies
  with whatever the stub returns.
* Pool runtime's RPC table closing over the boot ``backend``
  instead of the per-runtime ``rt_backend`` — pool sessions
  would silently route ``approve_plan`` to the wrong session.
* ``LifecycleController.startup`` forgetting to call
  ``RehydrateController.plan_decisions`` — silent loss of persisted
  decisions on restart, the original bug.

None of these would surface without these tests; ``validate_rpc_table``
only catches missing handlers, not wrong ones.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from igni_core.backend.server_lifecycle import LifecycleController
from igni_core.backend.server_rehydrate import RehydrateController
from igni_core.core.tools.plan import PlanDecisionsBlob, PlanStore
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.tools.todo import TodoItemWire, TodoStore
from igni_core.protocol.rpc import RpcMethod
from tests.rpc_tables import rpc_table_for


def _make_backend_with_recording_session() -> tuple[MagicMock, MagicMock]:
    """Construct a backend stub whose session's ``approve_plan`` /
    ``dismiss_plan`` are :class:`AsyncMock` so the test can introspect
    call args. The RPC handler calls ``backend.session.plan.approve``."""
    backend = MagicMock()
    backend.session.plan.approve = AsyncMock(
        return_value={"run_id": "R", "decision": "approved", "mode_status": ""}
    )
    backend.session.plan.dismiss = AsyncMock(
        return_value={"run_id": "R", "decision": "dismissed", "mode_status": ""}
    )
    return backend, backend.session


class TestApprovePlanRouting:
    async def test_lambda_calls_session_approve_plan_with_run_id(self):
        backend, session = _make_backend_with_recording_session()
        table = rpc_table_for(backend, MagicMock())

        result = table[RpcMethod.APPROVE_PLAN]({"run_id": "R-123"})
        # Lambdas return the coroutine — await it to drive the call.
        out = await result if asyncio.iscoroutine(result) else result

        session.plan.approve.assert_called_once_with(run_id="R-123")
        assert out == {"run_id": "R", "decision": "approved", "mode_status": ""}

    async def test_run_id_coerced_to_string(self):
        # If the wire envelope arrives with a non-string (e.g.
        # FE bug, MCP client sending an int by accident), the
        # dispatch must coerce so ``Session.plan.approve`` gets
        # the type it asserts on.
        backend, session = _make_backend_with_recording_session()
        table = rpc_table_for(backend, MagicMock())

        result = table[RpcMethod.APPROVE_PLAN]({"run_id": 42})
        await result if asyncio.iscoroutine(result) else result

        session.plan.approve.assert_called_once_with(run_id="42")

    async def test_missing_run_id_passes_empty_string(self):
        # ``args.get("run_id", "")`` is the contract — the
        # Session method itself raises on empty, which is the
        # right place to surface the error (not here). Pinning
        # this so a refactor doesn't silently turn the empty
        # default into ``None`` and break the assertion order.
        backend, session = _make_backend_with_recording_session()
        table = rpc_table_for(backend, MagicMock())

        result = table[RpcMethod.APPROVE_PLAN]({})
        await result if asyncio.iscoroutine(result) else result

        session.plan.approve.assert_called_once_with(run_id="")


class TestDismissPlanRouting:
    async def test_lambda_calls_session_dismiss_plan_with_run_id(self):
        backend, session = _make_backend_with_recording_session()
        table = rpc_table_for(backend, MagicMock())

        result = table[RpcMethod.DISMISS_PLAN]({"run_id": "R-789"})
        out = await result if asyncio.iscoroutine(result) else result

        session.plan.dismiss.assert_called_once_with(run_id="R-789")
        assert out["decision"] == "dismissed"

    async def test_routes_to_dismiss_NOT_approve(self):
        # Sanity: copy-paste regression where APPROVE_PLAN and
        # DISMISS_PLAN both end up calling ``approve_plan``
        # would pass the previous test (which only checks
        # approve) but break this one. Catches the wrong-method
        # bug specifically.
        backend, session = _make_backend_with_recording_session()
        table = rpc_table_for(backend, MagicMock())

        result = table[RpcMethod.DISMISS_PLAN]({"run_id": "R-xyz"})
        await result if asyncio.iscoroutine(result) else result

        session.plan.approve.assert_not_called()
        session.plan.dismiss.assert_called_once()


class TestPoolRuntimeRouting:
    """Pool sessions get their own ``RpcRouter.build_table`` call with
    ``rt_backend`` — pinning that the lambda closure binds to that
    backend, not the boot one. The bug shape: a refactor changes
    ``backend`` (closure capture) to ``self.backend`` (attribute
    lookup) and silently uses the wrong instance for pool runtimes.
    """

    async def test_pool_runtime_table_routes_to_pool_session(self):
        boot_backend, boot_session = _make_backend_with_recording_session()
        pool_backend, pool_session = _make_backend_with_recording_session()

        # Boot table. Built but not stored — the assertion that
        # matters is that the POOL table's lambda hits the POOL
        # session only. Constructing the boot table proves the
        # two builders don't share mutable state, which is the
        # closure-isolation behaviour we want to pin.
        rpc_table_for(boot_backend, MagicMock())
        # Pool table — separate instance built against the
        # runtime's backend.
        pool_table = rpc_table_for(pool_backend, MagicMock())

        # Fire ``approve_plan`` against the POOL table only.
        result = pool_table[RpcMethod.APPROVE_PLAN]({"run_id": "pool-run"})
        await result if asyncio.iscoroutine(result) else result

        # Only the pool session saw the call. The boot session
        # is completely untouched — if the lambda was sharing a
        # closure, the boot session would have been hit too.
        pool_session.plan.approve.assert_called_once_with(run_id="pool-run")
        boot_session.plan.approve.assert_not_called()

    async def test_boot_and_pool_tables_are_independent(self):
        # Reverse direction — boot RPC must not reach the pool
        # session either.
        boot_backend, boot_session = _make_backend_with_recording_session()
        pool_backend, pool_session = _make_backend_with_recording_session()

        boot_table = rpc_table_for(boot_backend, MagicMock())
        _ = rpc_table_for(pool_backend, MagicMock())

        result = boot_table[RpcMethod.APPROVE_PLAN]({"run_id": "boot-run"})
        await result if asyncio.iscoroutine(result) else result

        boot_session.plan.approve.assert_called_once_with(run_id="boot-run")
        pool_session.plan.approve.assert_not_called()


# ── Startup wires RehydrateController.plan_decisions ─────────


def _lifecycle(session: SimpleNamespace) -> LifecycleController:
    """A :class:`LifecycleController` whose plan-decision and todo
    rehydrate steps are real; every other startup step is stubbed so
    the session only needs what those two read."""
    rehydrate = RehydrateController(session, ProcessSupervisor())  # type: ignore[arg-type]
    rehydrate.plan_store = AsyncMock()  # type: ignore[method-assign]
    rehydrate.event_log = AsyncMock()  # type: ignore[method-assign]
    rehydrate.orphan_processes = AsyncMock()  # type: ignore[method-assign]
    lifecycle = LifecycleController(
        session=session,  # type: ignore[arg-type]
        pending_store=None,
        runs=None,
        rehydrate=rehydrate,
    )
    lifecycle.detect_interrupted_run = AsyncMock()  # type: ignore[method-assign]
    return lifecycle


class TestStartupRehydratesPlanDecisions:
    """``LifecycleController.startup`` must call
    ``RehydrateController.plan_decisions`` or persisted approvals
    silently vanish on restart — the original "I've never approved
    it" bug shape. The unit tests pin the rehydrate method's
    behaviour; this pins that startup actually calls it.
    """

    async def test_startup_calls_rehydrate_plan_decisions(self):
        session = SimpleNamespace(
            loop=SimpleNamespace(load_persisted_loop_state=AsyncMock()),
            plan_store=PlanStore(),
            todo_store=TodoStore(),
            persistence=SimpleNamespace(
                load_plan_decisions=AsyncMock(
                    return_value=PlanDecisionsBlob(decisions={"r1": "approved"})
                ),
                load_todos=AsyncMock(return_value=[]),
            ),
        )

        await _lifecycle(session).startup()

        # After startup the decision is in the store — proves
        # the rehydrate ran AND the persistence path was
        # exercised.
        assert session.plan_store.decisions == {"r1": "approved"}

    async def test_startup_calls_rehydrate_todos(self):
        # Same shape for todos — todo execution state must
        # survive restart, which means startup has to load it.
        todo_store = TodoStore()
        session = SimpleNamespace(
            loop=SimpleNamespace(load_persisted_loop_state=AsyncMock()),
            plan_store=PlanStore(),
            todo_store=todo_store,
            persistence=SimpleNamespace(
                load_plan_decisions=AsyncMock(return_value=PlanDecisionsBlob()),
                load_todos=AsyncMock(
                    return_value=[TodoItemWire(content="Task A", status="in_progress")]
                ),
            ),
        )

        await _lifecycle(session).startup()

        assert todo_store.items == [TodoItemWire(content="Task A", status="in_progress")]

    async def test_startup_survives_persistence_failure(self):
        # If ``load_plan_decisions`` raises (DB corruption,
        # I/O blip), startup must NOT crash. The user lands in
        # a session that pretends no decisions were ever made
        # — degraded but functional, same as a fresh boot.
        session = SimpleNamespace(
            loop=SimpleNamespace(load_persisted_loop_state=AsyncMock()),
            plan_store=PlanStore(),
            todo_store=TodoStore(),
            persistence=SimpleNamespace(
                load_plan_decisions=AsyncMock(side_effect=RuntimeError("DB blew up")),
                load_todos=AsyncMock(return_value=[]),
            ),
        )

        # Must not raise — the whole point of best-effort
        # persistence is graceful degradation on restart.
        await _lifecycle(session).startup()

        # Empty decisions — load failed, fall back to "nothing
        # recorded yet".
        assert session.plan_store.decisions == {}
