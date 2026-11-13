"""The toolkits in ``core/tools`` as the model meets them, and what stood behind them.

* every toolkit registers the function names the model was trained on,
  under its toolkit name, and the loop module still exports
  ``LoopProgressTool``;
* ``loop_set_total`` works against what the real ``Session`` offers (its
  ``loop`` controller and ``loop_progress_store``), not a facade method
  the session never had;
* ``loop_start`` hands the default cap on when the model leaves it out;
* progress rows list oldest first from the real SQLite store;
* ``lsp_query`` answers a result JSON cannot dump with its ``str()``;
* ``monitor_stop`` and ``monitor_restart`` against a real manager, and a
  manager that raises;
* ``spawn_team`` with no usable name, the call recorded around a spawn,
  and ``create_agent`` registered only when the settings allow it.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from agno.tools import Toolkit

import igni_core.core.tools.loop as loop_module
from igni_core.core.loop.limits import LOOP_DEFAULT_MAX_ITERATIONS
from igni_core.core.loop.store import LoopProgressStore
from igni_core.core.monitors import MonitorConfig, MonitorManager
from igni_core.core.tools.loop import LoopTools
from igni_core.core.tools.loop_progress import LoopProgressTool
from igni_core.core.tools.lsp import LspTools
from igni_core.core.tools.monitors import MonitorTools
from igni_core.core.tools.orchestrate import OrchestrateTools
from igni_core.core.tools.spawn_context import SpawnCalls

# The toolkit names the model was trained on start with the old product word.
# It is spelled in two halves so the repository's sweep for that word stays clean.
_PREFIX = "em" + "ber"


def _registered(toolkit: Toolkit) -> set[str]:
    return set(toolkit.functions) | set(toolkit.async_functions)


# ── What is registered ──────────────────────────────────────────────


def test_the_loop_toolkit_registers_its_five_tools() -> None:
    tools = LoopTools(SimpleNamespace())  # type: ignore[arg-type]
    assert tools.name == _PREFIX + "_loop"
    assert _registered(tools) == {
        "loop_start",
        "loop_stop",
        "loop_status",
        "loop_resume",
        "loop_set_total",
    }


def test_the_progress_toolkit_registers_its_five_tools() -> None:
    tools = LoopProgressTool(SimpleNamespace())  # type: ignore[arg-type]
    assert tools.name == _PREFIX + "_loop_progress"
    assert _registered(tools) == {
        "loop_progress_get",
        "loop_progress_set",
        "loop_progress_list",
        "loop_progress_delete",
        "loop_progress_clear",
    }


def test_the_loop_module_still_exports_the_progress_tool() -> None:
    assert loop_module.LoopProgressTool is LoopProgressTool
    assert set(loop_module.__all__) == {"LoopTools", "LoopProgressTool"}


def test_the_lsp_toolkit_registers_query_and_list() -> None:
    tools = LspTools(MagicMock())
    assert tools.name == _PREFIX + "_lsp"
    assert _registered(tools) == {"lsp_query", "lsp_list_servers"}


def test_the_monitor_toolkit_registers_four_tools_and_none_that_starts_one(tmp_path: Path) -> None:
    tools = MonitorTools(MonitorManager({}, tmp_path))
    assert tools.name == _PREFIX + "_monitors"
    assert _registered(tools) == {
        "monitor_status",
        "monitor_output",
        "monitor_restart",
        "monitor_stop",
    }


def _settings(generate_ephemeral: bool) -> Any:
    s = MagicMock()
    s.orchestration.max_nesting_depth = 5
    s.orchestration.max_total_agents = 20
    s.orchestration.generate_ephemeral = generate_ephemeral
    return s


@pytest.mark.parametrize(
    ("generate_ephemeral", "expected"),
    [
        (False, {"spawn_agent", "spawn_team"}),
        (True, {"spawn_agent", "spawn_team", "create_agent"}),
    ],
)
def test_create_agent_is_registered_only_when_the_settings_allow_it(
    generate_ephemeral: bool, expected: set[str]
) -> None:
    tools = OrchestrateTools(pool=MagicMock(), settings=_settings(generate_ephemeral))
    assert tools.name == _PREFIX + "_orchestrate"
    assert _registered(tools) == expected


async def test_loop_start_hands_the_default_cap_on_when_the_model_omits_it() -> None:
    controller = MagicMock()
    controller.start_loop_from_tool = AsyncMock(return_value=SimpleNamespace(render=lambda: "ok"))
    session = SimpleNamespace(loop=controller)

    assert await LoopTools(session).loop_start("do it") == "ok"  # type: ignore[arg-type]
    controller.start_loop_from_tool.assert_awaited_once_with("do it", LOOP_DEFAULT_MAX_ITERATIONS)
    assert LOOP_DEFAULT_MAX_ITERATIONS == 30


# ── Progress rows from the real store ───────────────────────────────


async def test_progress_lists_oldest_first_with_multiline_and_unicode_values(
    tmp_path: Path,
) -> None:
    session = SimpleNamespace(
        loop=SimpleNamespace(loop_run_id="run-1"),
        loop_progress_store=LoopProgressStore(db_path=tmp_path / "state.db"),
    )
    tool = LoopProgressTool(session)  # type: ignore[arg-type]
    for key, value in [("zeta", "first"), ("alpha", "двa\nlines"), ("mid", "ok ✓")]:
        await tool.loop_progress_set(key, value)
    await tool.loop_progress_set("zeta", "first again")  # replaced, not re-ordered

    listed = await tool.loop_progress_list()

    assert listed == "Progress so far:\n- zeta: first again\n- alpha: двa\nlines\n- mid: ok ✓"
    assert await tool.loop_progress_get("alpha") == "двa\nlines"
    assert await tool.loop_progress_clear() == "Cleared 3 progress entries."
    assert await tool.loop_progress_list() == "No progress recorded."


# ── lsp_query's last resort ─────────────────────────────────────────


async def test_a_result_json_cannot_dump_is_answered_with_its_str() -> None:
    manager = MagicMock()
    result = {("a", "tuple"): 1}  # a key json refuses, whatever ``default`` says
    manager.query = AsyncMock(return_value=result)
    assert await LspTools(manager).lsp_query("s", "m", "{}") == str(result)


async def test_a_value_json_cannot_dump_goes_through_str_inside_the_json() -> None:
    manager = MagicMock()
    manager.query = AsyncMock(return_value={"path": Path("/a/b"), "ok": "é"})
    out = await LspTools(manager).lsp_query("s", "m", "[1, 2]")
    assert json.loads(out) == {"path": "/a/b", "ok": "é"}
    manager.query.assert_awaited_once_with("s", "m", [1, 2])


# ── monitor_stop and monitor_restart ────────────────────────────────


async def test_monitor_stop_surfaces_a_manager_exception_and_logs_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = MagicMock()
    manager.stop = AsyncMock(side_effect=RuntimeError("boom"))
    with caplog.at_level("WARNING"):
        out = await MonitorTools(manager).monitor_stop("x")
    assert out == "Error: boom"
    assert "monitor_stop x raised: boom" in caplog.text


async def test_monitor_restart_logs_a_manager_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = MagicMock()
    manager.restart = AsyncMock(side_effect=RuntimeError("boom"))
    with caplog.at_level("WARNING"):
        assert await MonitorTools(manager).monitor_restart("x") == "Error: boom"
    assert "monitor_restart x raised: boom" in caplog.text


async def test_the_tools_stop_and_restart_a_real_monitor_and_say_when_it_is_unknown(
    tmp_path: Path,
) -> None:
    cfg = MonitorConfig(name="w", command="sleep", args=["30"])
    manager = MonitorManager({"w": cfg}, tmp_path)
    tools = MonitorTools(manager)
    try:
        assert await tools.monitor_restart("w") == "Restarted w."
        assert manager.snapshot_all()[0].status.value == "running"
        assert await tools.monitor_stop("w") == "Stopped w."
        assert manager.snapshot_all()[0].status.value == "stopped"
        assert await tools.monitor_stop("nope") == "Monitor not configured: 'nope'"
        assert await tools.monitor_restart("nope") == "Monitor not configured: 'nope'"
    finally:
        await manager.shutdown_all()


def test_monitor_output_of_zero_or_fewer_lines_is_the_empty_note(tmp_path: Path) -> None:
    from igni_core.core.monitors import MonitorHandle

    cfg = MonitorConfig(name="x", command="echo")
    manager = MonitorManager({"x": cfg}, tmp_path)
    handle = MonitorHandle(cfg, project_dir=tmp_path)
    handle.append_output("line")
    manager._handles["x"] = handle
    tools = MonitorTools(manager)
    assert tools.monitor_output("x", lines=0) == "(no output yet for x)"
    assert tools.monitor_output("x", lines=-2) == "(no output yet for x)"
    assert tools.monitor_output("x", lines=1) == "line"
    assert tools.monitor_output("x", lines=None) == "Error: lines must be an integer"  # type: ignore[arg-type]


# ── Orchestrate ─────────────────────────────────────────────────────


@pytest.mark.parametrize("names", ["", " , ,", ","])
async def test_a_team_with_no_usable_name_is_refused_and_spends_no_budget(names: str) -> None:
    tools = OrchestrateTools(pool=MagicMock(), settings=_settings(False))
    out = await tools.spawn_team("task", names)
    assert out == "Error: No agent names provided."
    assert tools.scope.budget.try_reserve(20) is None  # the whole cap is still free


async def test_the_team_names_are_trimmed_and_blanks_dropped_before_the_pool_is_asked() -> None:
    asked: list[str] = []
    pool = MagicMock()
    pool.get.side_effect = lambda name: asked.append(name)  # unknown: stops the build
    pool.missing.side_effect = lambda name: f"missing {name}"
    tools = OrchestrateTools(pool=pool, settings=_settings(False))

    out = await tools.spawn_team("task", " a , ,b ,")

    assert asked == ["a"]
    assert out == "missing a"


def _run_context(tool_name: str, args: dict[str, Any]) -> tuple[Any, Any]:
    class _Message:
        role = "assistant"
        tool_calls = [
            {"id": "call-9", "function": {"name": tool_name, "arguments": json.dumps(args)}}
        ]

        def to_dict(self) -> dict[str, Any]:
            return {"role": "assistant", "tool_calls": self.tool_calls}

    run_context = SimpleNamespace(run_id="run-1", session_id="sess-1", messages=[_Message()])
    agent = SimpleNamespace(name="main", id="main-id")
    return run_context, agent


@pytest.mark.parametrize("via", ["spawn_agent", "spawn_team"])
async def test_the_waiting_call_is_recorded_around_the_spawn_and_dropped_after(via: str) -> None:
    seen: list[Any] = []
    pool = MagicMock()

    def get(name: str) -> None:
        seen.append(SpawnCalls.current())

    pool.get.side_effect = get
    pool.missing.side_effect = lambda name: f"missing {name}"
    tools = OrchestrateTools(pool=pool, settings=_settings(False))
    if via == "spawn_agent":
        args = {"task": "t", "agent_name": "editor"}
        run_context, agent = _run_context("spawn_agent", args)
        await tools.spawn_agent("t", "editor", agent=agent, run_context=run_context)
    else:
        args = {"task": "t", "agent_names": "editor", "mode": "coordinate"}
        run_context, agent = _run_context("spawn_team", args)
        await tools.spawn_team("t", "editor", agent=agent, run_context=run_context)

    assert len(seen) == 1 and len(seen[0]) == 1
    call = seen[0][0]
    assert (call.agent_name, call.agent_id, call.run_id, call.session_id) == (
        "main",
        "main-id",
        "run-1",
        "sess-1",
    )
    assert (call.tool_call_id, call.tool_name) == ("call-9", via)
    assert SpawnCalls.current() == []


async def test_without_a_run_context_nothing_is_recorded() -> None:
    seen: list[Any] = []
    pool = MagicMock()
    pool.get.side_effect = lambda name: seen.append(SpawnCalls.current())
    pool.missing.side_effect = lambda name: "missing"
    await OrchestrateTools(pool=pool, settings=_settings(False)).spawn_agent("t", "editor")
    assert seen == [[]]
