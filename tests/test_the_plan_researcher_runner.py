"""The plan researcher runner spawns ``plan_researcher`` and never breaks planning.

``enter_plan_mode(task=...)`` shows the agent the researcher's report.
When the spawn cannot happen, the runner answers an empty string and
the agent researches by hand; cancellation is never swallowed. These
drive the runner itself, with real :class:`OrchestrateTools`-typed
stand-ins, so the discovery, the cache and the exception policy are
what is asserted.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.core.tools.orchestrate import OrchestrateTools
from igni_core.core.tools.plan import PlanResearcherRunner


def _orchestrate(*, spawn: AsyncMock | None = None, registered: bool = True) -> MagicMock:
    orch = MagicMock()
    orch.__class__ = OrchestrateTools
    orch.spawn_agent = spawn or AsyncMock(return_value="report")
    orch.pool.get_entry.return_value = MagicMock() if registered else None
    return orch


def _runner(*tools: object) -> PlanResearcherRunner:
    return PlanResearcherRunner(SimpleNamespace(main_team=SimpleNamespace(tools=list(tools))))


async def test_the_report_is_what_the_spawn_answered():
    orch = _orchestrate(spawn=AsyncMock(return_value="## Findings"))

    assert await _runner(orch).run("look") == "## Findings"
    orch.spawn_agent.assert_awaited_once_with(task="look", agent_name="plan_researcher")
    orch.pool.get_entry.assert_called_once_with("plan_researcher")


async def test_a_spawn_result_that_is_not_text_is_stringified():
    orch = _orchestrate(spawn=AsyncMock(return_value={"a": 1}))

    assert await _runner(orch).run("look") == "{'a': 1}"


async def test_no_team_no_tools_or_no_orchestrate_tools_answers_empty():
    for session in (
        SimpleNamespace(),
        SimpleNamespace(main_team=None),
        SimpleNamespace(main_team=SimpleNamespace(tools=None)),
        SimpleNamespace(main_team=SimpleNamespace(tools=[])),
        SimpleNamespace(main_team=SimpleNamespace(tools=[object()])),
    ):
        assert await PlanResearcherRunner(session).run("look") == ""


async def test_an_agent_missing_from_the_pool_is_not_spawned():
    orch = _orchestrate(registered=False)

    assert await _runner(orch).run("look") == ""
    orch.spawn_agent.assert_not_awaited()


@pytest.mark.parametrize("error", [LookupError("x"), ValueError("x"), RuntimeError("x")])
async def test_the_documented_spawn_failures_fall_back_to_manual_research(error):
    orch = _orchestrate(spawn=AsyncMock(side_effect=error))

    assert await _runner(orch).run("look") == ""


@pytest.mark.parametrize("error", [OSError("x"), TimeoutError("x")])
async def test_an_unlisted_spawn_failure_propagates(error):
    orch = _orchestrate(spawn=AsyncMock(side_effect=error))

    with pytest.raises(type(error)):
        await _runner(orch).run("look")


async def test_cancellation_is_not_swallowed():
    orch = _orchestrate(spawn=AsyncMock(side_effect=asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await _runner(orch).run("look")


async def test_the_first_orchestrate_tools_wins_among_several_tools():
    first, second = _orchestrate(), _orchestrate()

    await _runner(object(), first, second).run("look")

    first.spawn_agent.assert_awaited_once()
    second.spawn_agent.assert_not_awaited()


async def test_the_discovery_is_cached_until_invalidated():
    old, new = _orchestrate(), _orchestrate()
    team = SimpleNamespace(tools=[old])
    runner = PlanResearcherRunner(SimpleNamespace(main_team=team))
    await runner.run("one")

    team.tools = [new]  # a rebuilt team
    await runner.run("two")
    assert old.spawn_agent.await_count == 2
    new.spawn_agent.assert_not_awaited()

    runner.invalidate()
    await runner.run("three")
    new.spawn_agent.assert_awaited_once_with(task="three", agent_name="plan_researcher")


async def test_a_late_arriving_team_is_found_on_the_next_call():
    session = SimpleNamespace(main_team=None)
    runner = PlanResearcherRunner(session)
    assert await runner.run("one") == ""

    orch = _orchestrate()
    session.main_team = SimpleNamespace(tools=[orch])

    assert await runner.run("two") == "report"
