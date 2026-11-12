"""``LifecycleController``: the start-up order, interrupted-run hand-over and
the one teardown every caller shares.

``test_shutdown_is_the_window_s_and_happens_once`` pins that a second
``shutdown`` call runs nothing again; ``test_crash_survival`` pins the
hand-over against real stores. This file pins the order start-up does its
work in, what a failing first step does, the hand-over's absent cases, the
optional collaborators, and that callers asking for the teardown *at the
same time* still get one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.backend.schemas_lifecycle import RehydrateOutcome
from igni_core.backend.server_lifecycle import LifecycleController
from igni_core.core.session.pending_messages import PendingMessageStore


class _Rehydrate:
    """Records the order its steps are asked in."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self.queue_given: object = "unset"

    async def _step(self, name: str) -> RehydrateOutcome:
        self._calls.append(name)
        return RehydrateOutcome(ok=True, step=name)  # type: ignore[arg-type]

    async def plan_store(self) -> RehydrateOutcome:
        return await self._step("plan_store")

    async def plan_decisions(self) -> RehydrateOutcome:
        return await self._step("plan_decisions")

    async def todos(self) -> RehydrateOutcome:
        return await self._step("todos")

    async def event_log(self) -> RehydrateOutcome:
        return await self._step("event_log")

    async def orphan_processes(self) -> RehydrateOutcome:
        return await self._step("orphan_processes")

    async def queued_messages(self, queue: object) -> RehydrateOutcome:
        self.queue_given = queue
        return await self._step("queued_messages")


def _session(calls: list[str], *, hook: AsyncMock | None = None) -> SimpleNamespace:
    async def load() -> None:
        calls.append("loop_state")

    return SimpleNamespace(
        session_id="sess",
        loop=SimpleNamespace(load_persisted_loop_state=load),
        main_team=SimpleNamespace(aget_session=AsyncMock(return_value=None)),
        hook_executor=SimpleNamespace(execute=hook or AsyncMock()),
        settings=SimpleNamespace(orchestration=SimpleNamespace(auto_cleanup=False)),
        pool=SimpleNamespace(has_ephemeral=False),
        mcp_manager=SimpleNamespace(disconnect_all=AsyncMock()),
        supervisor=SimpleNamespace(registry=MagicMock()),
    )


class _Runs:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self.handed: list[object] = []

    def set_interrupted_summary(self, summary: object) -> None:
        self._calls.append("hand_over")
        self.handed.append(summary)


def _controller(calls: list[str], **kw) -> tuple[LifecycleController, _Rehydrate]:  # type: ignore[no-untyped-def]
    rehydrate = _Rehydrate(calls)
    kw.setdefault("pending_store", None)
    kw.setdefault("runs", None)
    session = kw.pop("session", None) or _session(calls)
    return LifecycleController(session=session, rehydrate=rehydrate, **kw), rehydrate  # type: ignore[arg-type]


class TestStartUp:
    async def test_it_loads_the_loop_then_looks_for_a_crash_then_rehydrates_in_order(
        self, tmp_path: Path
    ) -> None:
        calls: list[str] = []
        store = PendingMessageStore(tmp_path / "state.db")
        store.record_received("sess", "unfinished")
        runs = _Runs(calls)
        controller, _ = _controller(calls, pending_store=store, runs=runs)

        await controller.startup()

        assert calls == [
            "loop_state",
            "hand_over",
            "plan_store",
            "plan_decisions",
            "todos",
            "event_log",
            "orphan_processes",
            "queued_messages",
        ]

    async def test_the_queue_it_was_given_is_the_one_rehydrated(self) -> None:
        calls: list[str] = []
        queue = object()
        controller, rehydrate = _controller(calls, message_queue=queue)

        await controller.startup()

        assert rehydrate.queue_given is queue

    async def test_with_no_queue_none_is_rehydrated(self) -> None:
        calls: list[str] = []
        controller, rehydrate = _controller(calls)

        await controller.startup()

        assert rehydrate.queue_given is None

    async def test_a_loop_state_that_will_not_load_stops_start_up_before_anything_else(
        self,
    ) -> None:
        calls: list[str] = []
        session = _session(calls)
        session.loop.load_persisted_loop_state = AsyncMock(side_effect=RuntimeError("corrupt"))
        controller, _ = _controller(calls, session=session)

        with pytest.raises(RuntimeError, match="corrupt"):
            await controller.startup()

        assert calls == []

    async def test_a_failed_rehydrate_step_is_an_outcome_not_an_abort(self, caplog) -> None:  # type: ignore[no-untyped-def]
        calls: list[str] = []
        controller, rehydrate = _controller(calls)

        async def failed() -> RehydrateOutcome:
            calls.append("todos")
            return RehydrateOutcome(ok=False, step="todos", reason="disk full")

        rehydrate.todos = failed  # type: ignore[method-assign]
        with caplog.at_level("WARNING"):
            await controller.startup()

        assert calls[-3:] == ["event_log", "orphan_processes", "queued_messages"]
        assert "todos=disk full" in caplog.text


class TestHandingOverAnInterruptedRun:
    async def test_no_interruption_hands_nothing_over(self, tmp_path: Path) -> None:
        calls: list[str] = []
        runs = _Runs(calls)
        controller, _ = _controller(
            calls, pending_store=PendingMessageStore(tmp_path / "state.db"), runs=runs
        )

        assert await controller.detect_interrupted_run() is None
        assert runs.handed == []

    async def test_a_summary_is_returned_and_handed_to_the_run_controller(
        self, tmp_path: Path
    ) -> None:
        calls: list[str] = []
        store = PendingMessageStore(tmp_path / "state.db")
        store.record_received("sess", "unfinished")
        runs = _Runs(calls)
        controller, _ = _controller(calls, pending_store=store, runs=runs)

        summary = await controller.detect_interrupted_run()

        assert summary is not None
        assert "unfinished" in summary.recap
        assert runs.handed == [summary]

    async def test_with_no_run_controller_the_summary_is_still_returned(
        self, tmp_path: Path
    ) -> None:
        calls: list[str] = []
        store = PendingMessageStore(tmp_path / "state.db")
        store.record_received("sess", "unfinished")
        controller, _ = _controller(calls, pending_store=store, runs=None)

        assert await controller.detect_interrupted_run() is not None

    async def test_with_no_pending_store_a_clean_start_finds_nothing(self) -> None:
        controller, _ = _controller([], pending_store=None)

        assert await controller.detect_interrupted_run() is None


class TestShutDown:
    async def test_a_scheduler_stop_is_part_of_the_teardown(self) -> None:
        stop = AsyncMock()
        controller, _ = _controller([], scheduler_stop=stop)

        result = await controller.shutdown()

        stop.assert_awaited_once_with()
        assert result.scheduler_stopped is True
        assert result.hook_fired and result.mcp_disconnected and result.pool_cleaned

    async def test_without_one_the_scheduler_is_simply_not_stopped(self) -> None:
        controller, _ = _controller([])

        result = await controller.shutdown()

        assert result.scheduler_stopped is False
        assert result.errors == []

    async def test_a_failing_step_is_recorded_and_the_rest_still_run(self) -> None:
        calls: list[str] = []
        session = _session(calls)
        session.mcp_manager.disconnect_all = AsyncMock(side_effect=RuntimeError("socket gone"))
        stop = AsyncMock()
        controller, _ = _controller(calls, session=session, scheduler_stop=stop)

        result = await controller.shutdown()

        assert result.mcp_disconnected is False
        assert any("socket gone" in e for e in result.errors)
        assert result.scheduler_stopped is True

    async def test_callers_asking_at_the_same_time_share_one_teardown(self) -> None:
        release = asyncio.Event()

        async def slow_hook(*args: object, **kwargs: object) -> None:
            await release.wait()

        hook = AsyncMock(side_effect=slow_hook)
        session = _session([], hook=hook)
        controller, _ = _controller([], session=session)

        # The shutdown call, the pool's eviction and igni's exit all arrive
        # while the first is still in its SessionEnd hook.
        callers = [asyncio.create_task(controller.shutdown()) for _ in range(3)]
        await asyncio.sleep(0.05)
        release.set()
        results = await asyncio.gather(*callers)

        assert hook.await_count == 1
        assert session.mcp_manager.disconnect_all.await_count == 1
        assert results[0] is results[1] is results[2]
