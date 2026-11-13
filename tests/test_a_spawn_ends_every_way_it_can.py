"""The template every spawn follows, at each of its exits.

``SpawnRunner.run`` ends in one of six ways — refused before it starts,
refused by the budget, failed to build, finished, timed out, raised — and
cancellation is the seventh (``test_a_cancelled_spawn_cleans_up_after_itself``).
Each one hands the parent a different string, fires ``SubagentStop`` with
a different payload, and moves the budget differently.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.core.hooks.events import HookEvent
from igni_core.core.tools.orchestrate_budget import SpawnBudget
from igni_core.core.tools.orchestrate_spawn_runner import SpawnRunner
from igni_core.core.tools.orchestrate_spawn_types import (
    OrchestrateScope,
    SpawnContext,
    SpawnDeps,
    SpawnWiring,
    SubagentStartPayload,
    SubagentStopPayload,
)
from igni_core.core.tools.spawn_context import SpawnDeadline


class _Executor:
    def __init__(self) -> None:
        self.calls: list[tuple[HookEvent, Any]] = []

    async def execute(self, *, event: HookEvent, payload: Any) -> None:
        self.calls.append((event, payload))

    def of(self, event: HookEvent) -> list[Any]:
        return [p for e, p in self.calls if e is event]


def _deps() -> SpawnDeps:
    return SpawnDeps(
        agent_streamer=lambda context: None,
        team_streamer=lambda context: None,
        team_factory=lambda spec: None,
        model_registry_factory=lambda settings: None,
        nested_toolkit_factory=lambda spec: None,
    )


def _context(
    executor: _Executor | None = None,
    *,
    timeout: float = 3600,
    max_agents: int = 10,
    depth: int = 0,
    max_depth: int = 3,
    task: str = "t",
    budget: SpawnBudget | None = None,
) -> SpawnContext:
    scope = OrchestrateScope(
        pool=SimpleNamespace(),
        settings=SimpleNamespace(orchestration=SimpleNamespace(sub_team_timeout=timeout)),
        budget=budget or SpawnBudget(max_agents=max_agents),
        current_depth=depth,
        max_depth=max_depth,
        project_dir=None,
    )
    return SpawnContext(
        scope=scope,
        wiring=SpawnWiring(session_id="sess-1", hook_executor=executor),
        deps=_deps(),
        task=task,
    )


class _Runner(SpawnRunner):
    """A concrete runner whose steps a test sets."""

    def __init__(self, context: SpawnContext, **steps: Any) -> None:
        super().__init__(context)
        self.steps = steps
        self.executed = 0
        self.finally_calls = 0
        self.preflight_error: str | None = steps.get("preflight")
        self.build_error: str | None = steps.get("build")
        self.reserve = steps.get("reserve", 1)

    def _preflight(self) -> str | None:
        return self.preflight_error

    def _reserve_count(self) -> int:
        return self.reserve

    def _build(self) -> str | None:
        return self.build_error

    async def _execute(self) -> tuple[str, list[str]]:
        self.executed += 1
        return await self.steps["execute"]()

    def _agent_label(self) -> str:
        return "probe"

    def _start_mode(self) -> str | None:
        return self.steps.get("mode")

    def format_result(self, *, elapsed: float, result: str, activity: list[str]) -> str:
        if "format" in self.steps:
            return self.steps["format"]()
        return f"result={result}|activity={activity}"

    def _on_finally(self) -> None:
        self.finally_calls += 1


async def _done() -> tuple[str, list[str]]:
    return "answer", ["line"]


class TestRefusedBeforeItStarts:
    async def test_the_depth_limit_refuses_with_the_configured_number(self):
        budget = SpawnBudget(max_agents=10)
        runner = _Runner(_context(depth=3, max_depth=3, budget=budget), execute=_done)

        assert await runner.run() == "Error: Maximum nesting depth (3) reached."
        assert runner.executed == 0
        assert budget.count == 0

    async def test_a_depth_over_the_limit_is_refused_too(self):
        runner = _Runner(_context(depth=7, max_depth=3), execute=_done)

        assert await runner.run() == "Error: Maximum nesting depth (3) reached."

    async def test_a_preflight_error_is_returned_and_reserves_nothing(self):
        budget = SpawnBudget(max_agents=10)
        executor = _Executor()
        runner = _Runner(_context(executor, budget=budget), execute=_done, preflight="Error: nope")

        assert await runner.run() == "Error: nope"
        assert budget.count == 0
        assert executor.calls == []

    async def test_a_refused_budget_returns_its_limit_error_and_builds_nothing(self):
        budget = SpawnBudget(max_agents=2)
        executor = _Executor()
        runner = _Runner(_context(executor, budget=budget), execute=_done, reserve=3, build="never")

        assert await runner.run() == "Error: Maximum total agents (2) reached."
        assert budget.count == 0
        assert runner.executed == 0
        assert executor.calls == []

    async def test_a_build_error_returns_it_gives_every_slot_back_and_fires_no_hook(self):
        budget = SpawnBudget(max_agents=10)
        executor = _Executor()
        runner = _Runner(
            _context(executor, budget=budget), execute=_done, reserve=4, build="Error: no agent"
        )

        assert await runner.run() == "Error: no agent"
        assert budget.count == 0
        assert executor.calls == []
        assert runner.finally_calls == 0


class TestASpawnThatFinishes:
    async def test_it_announces_itself_runs_announces_its_end_and_formats(self):
        executor = _Executor()
        budget = SpawnBudget(max_agents=10)
        runner = _Runner(
            _context(executor, budget=budget, task="do the thing"),
            execute=_done,
            mode="route",
            reserve=2,
        )

        out = await runner.run()

        assert out == "result=answer|activity=['line']"
        assert budget.count == 2
        assert executor.calls == [
            (
                HookEvent.SUBAGENT_START,
                SubagentStartPayload(
                    session_id="sess-1", agent_name="probe", task="do the thing", mode="route"
                ),
            ),
            (
                HookEvent.SUBAGENT_STOP,
                SubagentStopPayload(
                    session_id="sess-1", agent_name="probe", result_preview="answer"
                ),
            ),
        ]
        assert runner.finally_calls == 1

    async def test_the_start_hook_clips_the_task_and_the_stop_hook_the_result_to_500(self):
        executor = _Executor()

        async def long() -> tuple[str, list[str]]:
            return "r" * 900, []

        runner = _Runner(_context(executor, task="x" * 900), execute=long)

        await runner.run()

        assert executor.of(HookEvent.SUBAGENT_START)[0].task == "x" * 500
        assert executor.of(HookEvent.SUBAGENT_START)[0].mode is None
        assert executor.of(HookEvent.SUBAGENT_STOP)[0].result_preview == "r" * 500

    async def test_an_empty_result_is_announced_as_empty(self):
        executor = _Executor()

        async def empty() -> tuple[str, list[str]]:
            return "", []

        await _Runner(_context(executor), execute=empty).run()

        assert executor.of(HookEvent.SUBAGENT_STOP)[0].result_preview == ""

    async def test_no_hook_executor_is_fine(self):
        assert await _Runner(_context(None), execute=_done).run() == (
            "result=answer|activity=['line']"
        )


class TestASpawnThatTimesOut:
    async def test_it_returns_the_timeout_message_and_stops_with_it(self):
        executor = _Executor()

        async def stall() -> tuple[str, list[str]]:
            await asyncio.sleep(3600)
            return "", []

        runner = _Runner(_context(executor, timeout=0.05), execute=stall)

        out = await runner.run()

        message = "Sub-spawn exceeded spawn timeout (0.05s) and was aborted."
        assert out == message
        assert executor.of(HookEvent.SUBAGENT_STOP) == [
            SubagentStopPayload(session_id="sess-1", agent_name="probe", error=message)
        ]
        assert runner.finally_calls == 1

    async def test_time_spent_waiting_on_a_person_does_not_count(self):
        """Approvals do not expire: the clock stops while a person decides."""

        async def approved_late() -> tuple[str, list[str]]:
            with SpawnDeadline.waiting_on_person():
                await asyncio.sleep(0.3)
            return "approved", []

        runner = _Runner(_context(timeout=0.1), execute=approved_late)

        assert await runner.run() == "result=approved|activity=[]"

    async def test_the_same_wait_without_the_pause_would_time_out(self):
        async def slow() -> tuple[str, list[str]]:
            await asyncio.sleep(0.3)
            return "late", []

        runner = _Runner(_context(timeout=0.1), execute=slow)

        assert "exceeded spawn timeout" in await runner.run()


class TestASpawnThatRaises:
    async def test_the_message_names_the_error_and_the_stop_carries_it(self):
        executor = _Executor()

        async def boom() -> tuple[str, list[str]]:
            raise ValueError("model fell over")

        runner = _Runner(_context(executor), execute=boom)

        out = await runner.run()

        assert out == "Error running sub-spawn: model fell over"
        assert executor.of(HookEvent.SUBAGENT_STOP) == [
            SubagentStopPayload(session_id="sess-1", agent_name="probe", error=out)
        ]
        assert runner.finally_calls == 1

    async def test_a_formatter_that_raises_is_an_error_after_a_clean_stop(self):
        """The stop already told the truth about the run; the formatter's
        failure is returned, and the stop is not fired a second time."""
        executor = _Executor()

        def bad() -> str:
            raise KeyError("shape")

        runner = _Runner(_context(executor), execute=_done, format=bad)

        out = await runner.run()

        assert out == "Error running sub-spawn: 'shape'"
        stops = executor.of(HookEvent.SUBAGENT_STOP)
        assert len(stops) == 1
        assert stops[0].result_preview == "answer"
        assert runner.finally_calls == 1


class TestASpawnThatIsCancelled:
    async def test_the_stop_says_it_was_cancelled_and_the_cancellation_goes_on(self):
        executor = _Executor()
        entered = asyncio.Event()

        async def hang() -> tuple[str, list[str]]:
            entered.set()
            await asyncio.sleep(3600)
            return "", []

        runner = _Runner(_context(executor), execute=hang)
        task = asyncio.create_task(runner.run())
        await asyncio.wait_for(entered.wait(), timeout=5)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert executor.of(HookEvent.SUBAGENT_STOP) == [
            SubagentStopPayload(
                session_id="sess-1", agent_name="probe", error="Sub-spawn was cancelled."
            )
        ]
        assert runner.finally_calls == 1


class TestTheBaseTemplateRefusesToGuess:
    @pytest.mark.parametrize(
        "call",
        [
            lambda r: asyncio.run(r._execute()),
            lambda r: r._agent_label(),
            lambda r: r.format_result(elapsed=0.0, result="", activity=[]),
        ],
    )
    def test_the_three_abstract_steps_raise(self, call):
        with pytest.raises(NotImplementedError):
            call(SpawnRunner(_context()))

    def test_the_defaults_for_the_optional_steps(self):
        runner = SpawnRunner(_context(task="y" * 700))

        assert runner._preflight() is None
        assert runner._reserve_count() == 1
        assert runner._build() is None
        assert runner._start_mode() is None
        assert runner._task_preview() == "y" * 500
