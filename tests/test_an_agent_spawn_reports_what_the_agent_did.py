"""``spawn_agent``: what the parent is told, and what the agent runs with.

The answer is a four-line header, the response, a warning when the run
died partway, any visualization the agent drew as a ``json-render``
fence, and the worktree footer. The agent runs as a per-spawn copy with
its tool output quarantined; an unknown isolation mode is refused rather
than silently giving no sandbox; a plugin agent can force a worktree; a
cancelled isolated spawn gives its worktree back.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.core.hooks.turn_results import TurnResultHook
from igni_core.core.hooks.untrusted import UntrustedOutputHook
from igni_core.core.tools.orchestrate_budget import SpawnBudget
from igni_core.core.tools.orchestrate_events import AgentStartedEvent
from igni_core.core.tools.orchestrate_spawn_agent import AgentSpawn
from igni_core.core.tools.orchestrate_spawn_types import (
    OrchestrateScope,
    SpawnContext,
    SpawnDeps,
    SpawnWiring,
)
from igni_core.core.tools.orchestrate_stream_context import StreamSinks
from igni_core.core.tools.orchestrate_symbols import VIS_SPEC_PREFIX


class _Agent:
    def __init__(self) -> None:
        self.name = "pooled"
        self.tools: list[Any] = []
        self.tool_hooks: list[Any] = ["pool-hook"]


class _Pool:
    def __init__(self, defn: Any, agent: Any | None = None) -> None:
        self.agent = agent if agent is not None else _Agent()
        self.defn = defn

    def get(self, name: str) -> Any:
        return self.agent if name == "worker" else None

    def get_entry(self, name: str) -> Any:
        return None if self.defn is None else SimpleNamespace(definition=self.defn)

    def missing(self, name: str) -> str:
        return f"Agent '{name}' not found"


def _defn(**overrides: Any) -> Any:
    fields = {
        "description": "Does the work",
        "tools": ["Read", "Write"],
        "force_isolation": None,
        "can_orchestrate": False,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class _Run:
    def __init__(self) -> None:
        self.contexts: list[Any] = []
        self.result: tuple[str, list[str]] = ("done", [])
        self.error: BaseException | None = None
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def streamer(self, context: Any) -> tuple[str, list[str]]:
        self.contexts.append(context)
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.result


def _spawn(
    pool: _Pool,
    run: _Run,
    *,
    isolation: str = "",
    project_dir: Path | None = None,
    sinks: StreamSinks | None = None,
    timeout: float = 30,
    task: str = "fix it",
    budget: SpawnBudget | None = None,
) -> AgentSpawn:
    scope = OrchestrateScope(
        pool=pool,
        settings=SimpleNamespace(orchestration=SimpleNamespace(sub_team_timeout=timeout)),
        budget=budget or SpawnBudget(max_agents=10),
        current_depth=0,
        max_depth=5,
        project_dir=project_dir,
    )
    deps = SpawnDeps(
        agent_streamer=run.streamer,
        team_streamer=lambda context: None,
        team_factory=lambda spec: None,
        model_registry_factory=lambda settings: None,
        nested_toolkit_factory=lambda spec: None,
    )
    context = SpawnContext(
        scope=scope,
        wiring=SpawnWiring(session_id="sess-1", hook_executor=None, sinks=sinks or StreamSinks()),
        deps=deps,
        task=task,
    )
    return AgentSpawn(context=context, agent_name="worker", isolation=isolation)


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "seed.txt").write_text("seed")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True)


class TestTheAnswer:
    async def test_it_is_the_header_then_the_response(self):
        run = _Run()
        run.result = ("all fixed", ["noise"])

        out = await _spawn(_Pool(_defn()), run, task="fix it").run()

        header, _, response = out.partition("\n\nResponse:\n")
        assert response == "all fixed"
        lines = header.split("\n")
        assert lines[0] == "[Agent: worker] Does the work"
        assert lines[1] == "[Tools: Read, Write]"
        assert lines[2] == "[Task: fix it]"
        assert lines[3].startswith("[Time: ") and lines[3].endswith("s]")
        assert len(lines) == 4

    @pytest.mark.parametrize("defn", [None, _defn(tools=[]), _defn(tools=None)])
    async def test_an_agent_with_no_listed_tools_says_none(self, defn):
        out = await _spawn(_Pool(defn), _Run()).run()

        assert "\n[Tools: none]\n" in out

    async def test_an_agent_without_a_definition_has_an_empty_description(self):
        out = await _spawn(_Pool(None), _Run()).run()

        assert out.startswith("[Agent: worker] \n")

    async def test_a_run_error_is_a_warning_under_the_partial_response(self):
        run = _Run()
        run.result = ("half", ["ok line", "RUN ERROR boom", "RUN ERROR second"])

        out = await _spawn(_Pool(_defn()), run).run()

        assert out.endswith(
            "Response:\nhalf\n\nWARNING: This sub-agent terminated with a run error — "
            "the response below is partial. Consider retrying, or proceed "
            "with the partial result if it's sufficient.\n"
            "RUN ERROR boom\nRUN ERROR second"
        )
        assert "ok line" not in out

    async def test_a_clean_run_has_no_warning(self):
        out = await _spawn(_Pool(_defn()), _Run()).run()

        assert "WARNING" not in out

    async def test_an_empty_result_is_still_an_answer(self):
        run = _Run()
        run.result = ("", [])

        out = await _spawn(_Pool(_defn()), run).run()

        assert out.endswith("\n\nResponse:\n")

    async def test_a_visualization_comes_back_as_a_json_render_fence_per_spec(self):
        run = _Run()
        run.result = (
            "chart below",
            [
                f"{VIS_SPEC_PREFIX}" + '{"a": "```"}',
                "unrelated",
                f"{VIS_SPEC_PREFIX}" + '{"b": 2}',
            ],
        )

        out = await _spawn(_Pool(_defn()), run).run()

        assert out.endswith(
            "chart below\n\nRendered for you — the block below is the visualization, "
            "ready to paste into a document as-is:\n\n"
            '```json-render\n{"a": "```"}\n```\n\n```json-render\n{"b": 2}\n```'
        )

    async def test_without_a_spec_there_is_no_fence(self):
        run = _Run()
        run.result = ("text", ["VISUALIZE something else"])

        assert "json-render" not in await _spawn(_Pool(_defn()), run).run()


class TestWhatTheAgentRunsWith:
    async def test_it_runs_a_copy_whose_tool_output_is_quarantined(self):
        pool = _Pool(_defn())
        run = _Run()

        await _spawn(pool, run).run()

        agent = run.contexts[0].runnable
        assert agent is not pool.agent
        assert isinstance(agent.tool_hooks[0], UntrustedOutputHook)
        assert agent.tool_hooks[1:] == ["pool-hook"]
        assert pool.agent.tool_hooks == ["pool-hook"]

    async def test_the_turn_result_hook_is_added_only_for_a_coordinator_that_can_take_it(self):
        class _Sink:
            def tool_finished(self, run_id: str, tool_call_id: str, result: str) -> None: ...

        with_sink = _Run()
        await _spawn(_Pool(_defn()), with_sink, sinks=StreamSinks(hitl_coordinator=_Sink())).run()
        without = _Run()
        await _spawn(_Pool(_defn()), without, sinks=StreamSinks(hitl_coordinator=object())).run()

        assert isinstance(with_sink.contexts[0].runnable.tool_hooks[-1], TurnResultHook)
        assert not any(
            isinstance(h, TurnResultHook) for h in without.contexts[0].runnable.tool_hooks
        )

    async def test_an_agent_without_a_hook_list_still_gets_the_quarantine(self):
        agent = _Agent()
        del agent.tool_hooks
        run = _Run()

        await _spawn(_Pool(_defn(), agent), run).run()

        (hook,) = run.contexts[0].runnable.tool_hooks
        assert isinstance(hook, UntrustedOutputHook)

    async def test_the_agent_started_event_names_the_agent_and_the_original_task(self):
        seen: list[AgentStartedEvent] = []
        spawn = _spawn(_Pool(_defn()), _Run(), sinks=StreamSinks(on_progress=seen.append))

        await spawn.run()

        assert [(e.agent, e.task, e.card_id) for e in seen] == [
            ("worker", "fix it", spawn._hooks.card_id)
        ]


class TestTheSpawnAsksForASandbox:
    async def test_an_unknown_isolation_mode_is_refused_and_costs_nothing(self):
        budget = SpawnBudget(max_agents=10)
        run = _Run()

        out = await _spawn(_Pool(_defn()), run, isolation="docker", budget=budget).run()

        assert out == "Error: unknown isolation mode 'docker'. Valid: ['worktree']."
        assert run.contexts == []
        assert budget.count == 0

    async def test_a_plugin_agent_forces_a_worktree_whatever_the_caller_asked(self, tmp_path):
        _init_git_repo(tmp_path)
        run = _Run()
        pool = _Pool(_defn(force_isolation="worktree"))

        out = await _spawn(pool, run, isolation="", project_dir=tmp_path).run()

        assert "isolated git worktree at" in run.contexts[0].task
        assert "(clean) — reaped." in out

    async def test_a_forced_worktree_without_a_project_is_the_spawns_error(self):
        run = _Run()
        pool = _Pool(_defn(force_isolation="worktree"))

        out = await _spawn(pool, run, isolation="", project_dir=None).run()

        assert out == "Error: isolation=worktree requires a project directory."
        assert run.contexts == []

    async def test_a_cancelled_isolated_spawn_removes_its_worktree(self, tmp_path):
        _init_git_repo(tmp_path)
        run = _Run()
        run.gate = asyncio.Event()
        spawn = _spawn(_Pool(_defn()), run, isolation="worktree", project_dir=tmp_path)
        task = asyncio.create_task(spawn.run())
        await asyncio.wait_for(run.entered.wait(), timeout=10)
        sandbox = spawn._state.sandbox
        assert sandbox is not None and sandbox.info is not None
        branch = sandbox.info.branch_name
        path = sandbox.info.worktree_path
        assert path.is_dir()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not path.exists()
        branches = subprocess.run(
            ["git", "branch", "--list", branch], cwd=tmp_path, capture_output=True, text=True
        ).stdout
        assert branches.strip() == ""


class TestTheOtherEnds:
    async def test_an_unknown_agent_is_the_pools_message_and_gives_the_slot_back(self):
        budget = SpawnBudget(max_agents=10)
        spawn = _spawn(_Pool(_defn()), _Run(), budget=budget)
        spawn._state.agent_name = "ghost"

        assert await spawn.run() == "Agent 'ghost' not found"
        assert budget.count == 0

    async def test_a_timeout_says_the_provider_likely_stalled(self):
        run = _Run()
        run.gate = asyncio.Event()

        out = await _spawn(_Pool(_defn()), run, timeout=0.05).run()

        assert out == (
            "Sub-agent 'worker' exceeded spawn timeout (0.05s) and was aborted. "
            "The model provider likely stalled mid-stream."
        )

    async def test_an_error_names_the_agent_and_the_cause(self):
        run = _Run()
        run.error = RuntimeError("provider 500")

        out = await _spawn(_Pool(_defn()), run).run()

        assert out == "Error running sub-agent 'worker': provider 500"

    async def test_an_isolated_spawn_that_fails_still_gives_its_worktree_back(self, tmp_path):
        _init_git_repo(tmp_path)
        run = _Run()
        run.error = RuntimeError("provider 500")
        spawn = _spawn(_Pool(_defn()), run, isolation="worktree", project_dir=tmp_path)

        out = await spawn.run()

        assert out == "Error running sub-agent 'worker': provider 500"
        sandbox = spawn._state.sandbox
        assert sandbox is not None and sandbox.info is not None
        assert not sandbox.info.worktree_path.exists()

    async def test_the_cleanup_hook_with_no_sandbox_does_nothing(self):
        spawn = _spawn(_Pool(_defn()), _Run())

        spawn._on_finally()  # must not raise
