"""A cancelled spawn still has to put its worktree back.

``SpawnRunner.run`` guarded the streaming await with ``except
asyncio.TimeoutError`` and ``except Exception`` and had no ``finally``.
``asyncio.CancelledError`` has been a ``BaseException`` since 3.8, so it
matched neither: a spawn cancelled mid-flight — session teardown, a
client disconnect, an outer ``wait_for`` — skipped both the
``SubagentStop`` hook (the FE's progress card never closes) and
``_on_finally`` (an ``isolation=worktree`` spawn leaves its git worktree
and branch on disk with nothing left holding a reference to them).

``AgentSpawn._on_finally`` was also an empty ``pass``, so even once the
template called it on every path there was nothing there to finalize.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from igni_core.core.tools.orchestrate_budget import SpawnBudget
from igni_core.core.tools.orchestrate_spawn_agent import AgentSpawn
from igni_core.core.tools.orchestrate_spawn_runner import SpawnRunner
from igni_core.core.tools.orchestrate_spawn_types import (
    OrchestrateScope,
    SpawnContext,
    SpawnDeps,
    SpawnWiring,
)


def _deps() -> SpawnDeps:
    """Factories no test here reaches — the runner never builds a runnable."""
    return SpawnDeps(
        agent_streamer=lambda context: None,
        team_streamer=lambda context: None,
        team_factory=lambda spec: None,
        model_registry_factory=lambda settings: None,
        nested_toolkit_factory=lambda spec: None,
    )


def _context(**overrides) -> SpawnContext:
    """A spawn context with nothing in it the template needs."""
    scope = OrchestrateScope(
        pool=SimpleNamespace(),
        settings=SimpleNamespace(orchestration=SimpleNamespace(sub_team_timeout=3600)),
        budget=SpawnBudget(max_agents=10),
        current_depth=0,
        max_depth=3,
        project_dir=None,
    )
    wiring = SpawnWiring(session_id="s1", hook_executor=None)
    kwargs = {"scope": scope, "wiring": wiring, "deps": _deps(), "task": "t"}
    kwargs.update(overrides)
    return SpawnContext(**kwargs)


class _Runner(SpawnRunner):
    """Minimal concrete runner whose ``_execute`` never returns."""

    def __init__(self, context: SpawnContext) -> None:
        super().__init__(context)
        self.finally_calls = 0
        self.entered = asyncio.Event()

    async def _execute(self) -> tuple[str, list[str]]:
        self.entered.set()
        await asyncio.sleep(3600)
        return "unreachable", []

    def _agent_label(self) -> str:
        return "probe"

    def format_result(self, *, elapsed: float, result: str, activity: list[str]) -> str:
        return result

    def _on_finally(self) -> None:
        self.finally_calls += 1


class _SpySandbox:
    def __init__(self) -> None:
        self.finalize_calls = 0

    def finalize(self) -> str:
        self.finalize_calls += 1
        return ""


async def test_cancellation_still_runs_the_cleanup_hook():
    runner = _Runner(_context())
    task = asyncio.create_task(runner.run())
    await asyncio.wait_for(runner.entered.wait(), timeout=5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runner.finally_calls == 1, (
        "_on_finally never ran on the cancellation path — an isolated spawn's "
        "worktree and branch are left on disk with nobody holding a reference"
    )


async def test_cancellation_still_fires_subagent_stop():
    runner = _Runner(_context())
    task = asyncio.create_task(runner.run())
    await asyncio.wait_for(runner.entered.wait(), timeout=5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runner._hooks.stop_fired is True, (
        "SubagentStop never fired, so the FE's team-progress card for this "
        "spawn stays open for the rest of the session"
    )


async def test_cancellation_still_propagates():
    """Cleaning up must not swallow the cancellation the caller asked for."""
    runner = _Runner(_context())
    task = asyncio.create_task(runner.run())
    await asyncio.wait_for(runner.entered.wait(), timeout=5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def test_the_success_path_still_runs_the_cleanup_hook_exactly_once():
    """The ``finally`` replaced three per-branch calls; not four, not two."""

    class _Fast(_Runner):
        async def _execute(self) -> tuple[str, list[str]]:
            self.entered.set()
            return "done", []

    runner = _Fast(_context())

    assert await runner.run() == "done"
    assert runner.finally_calls == 1


def _agent_spawn() -> AgentSpawn:
    return AgentSpawn(context=_context(), agent_name="a", isolation="none")


def test_agent_spawn_finalizes_its_sandbox_from_the_cleanup_hook():
    """The hook the template now calls on every path has to do something."""
    spawn = _agent_spawn()
    sandbox = _SpySandbox()
    spawn._state.sandbox = sandbox

    spawn._on_finally()

    assert sandbox.finalize_calls == 1


def test_the_cleanup_hook_tolerates_a_spawn_that_never_built_a_sandbox():
    spawn = _agent_spawn()

    spawn._on_finally()  # must not raise
