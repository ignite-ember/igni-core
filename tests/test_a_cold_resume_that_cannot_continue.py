"""A cold resume that cannot continue says so instead of going quiet.

``ColdSpawnResume`` picks up what a dead backend left waiting inside a
``spawn_agent`` / ``spawn_team`` call. When the pieces it needs are
gone — the link, an agent from the pool, the spawn tools, a team's
member — the person and the waiting call are told, and nothing is run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.backend.cold_spawn_resume import ColdSpawnResume
from igni_core.core.approvals.records import SpawnLink
from igni_core.core.config.settings import Settings
from igni_core.core.hitl.models import RestoredRun
from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator
from igni_core.core.tools.orchestrate import OrchestrateTools
from igni_core.core.tools.spawn_context import ParentCall
from igni_core.protocol import messages as msg

MAIN = "main"


class EmptyPool:
    """A pool with no agents in it."""

    db = None

    def get(self, name: str) -> None:
        return None

    def missing(self, name: str) -> str:
        return f"No agent named {name!r}."


class MainTeam:
    """The main agent: records the run it is asked to continue."""

    name = MAIN

    def __init__(self, tools: list[Any]) -> None:
        self.tools = tools
        self.continued: list[Any] = []

    async def acontinue_run(self, **kwargs: Any) -> AsyncIterator[msg.Message]:
        self.continued.append(kwargs["run_response"])
        return
        yield  # pragma: no cover


async def _through(stream: AsyncIterator[msg.Message]) -> AsyncIterator[msg.Message]:
    async for proto in stream:
        yield proto


def _session(tools: list[Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        main_team=MainTeam(tools or []),
        pool=EmptyPool(),
        sub_agent_hitl=SubAgentHITLCoordinator(),
    )


def _call(agent: str, tool: str, **args: Any) -> ParentCall:
    return ParentCall(
        agent_name=agent,
        run_id=f"run-{agent}",
        session_id="sess",
        tool_call_id=f"call-{agent}",
        tool_name=tool,
        tool_args=args,
        messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call-{agent}",
                        "function": {"name": tool, "arguments": "{}"},
                    }
                ],
            }
        ],
    )


def _link(*parents: ParentCall) -> SpawnLink:
    return SpawnLink(
        run_id="run-child",
        session_id="sess",
        agent_session_id="child-sess",
        agent_name="cleaner",
        parents=list(parents),
    )


def _ready(link: SpawnLink | None) -> RestoredRun:
    return RestoredRun(run_id="run-child", requirements=[], link=link)


async def _collect(stream: AsyncIterator[msg.Message]) -> list[msg.Message]:
    return [proto async for proto in stream]


def _given(session: SimpleNamespace) -> str:
    """The result the main agent's waiting call was handed."""
    (run,) = session.main_team.continued
    return str(run.requirements[0].tool_execution.result)


def _resume(session: SimpleNamespace) -> ColdSpawnResume:
    return ColdSpawnResume(session, _through)


@pytest.mark.asyncio
async def test_an_approval_with_no_link_is_recorded_and_the_person_told():
    session = _session()
    out = await _collect(_resume(session).resume(_ready(None)))
    assert [type(m) for m in out] == [msg.Info]
    assert "cannot be continued" in out[0].text
    assert session.main_team.continued == []


@pytest.mark.asyncio
async def test_an_approval_whose_link_has_no_waiting_call_is_recorded_and_the_person_told():
    session = _session()
    out = await _collect(_resume(session).resume(_ready(_link())))
    assert [type(m) for m in out] == [msg.Info]
    assert "cannot be continued" in out[0].text


@pytest.mark.asyncio
async def test_a_sub_agent_missing_from_the_pool_is_named_and_not_continued():
    session = _session()
    parent = _call(MAIN, "spawn_agent", task="clean", agent_name="cleaner")
    out = await _collect(_resume(session).resume(_ready(_link(parent))))
    assert [type(m) for m in out] == [msg.Error]
    assert "'cleaner'" in out[0].text
    assert session.main_team.continued == []


@pytest.mark.asyncio
async def test_a_caller_missing_from_the_pool_is_named_and_the_chain_stops():
    session = _session()
    middle = _call("planner", "spawn_agent", task="plan", agent_name="cleaner")
    top = _call(MAIN, "spawn_agent", task="all", agent_name="planner")
    out = await _collect(_resume(session).interrupted(_link(middle, top)))
    assert [type(m) for m in out] == [msg.Error]
    assert "'planner'" in out[0].text
    assert session.main_team.continued == []


@pytest.mark.asyncio
async def test_an_interrupted_spawn_with_no_waiting_call_is_only_forgotten():
    session = _session()
    out = await _collect(_resume(session).interrupted(_link()))
    assert out == []
    assert session.main_team.continued == []


@pytest.mark.asyncio
async def test_a_team_in_a_session_without_spawn_tools_is_reported_to_the_waiting_call():
    session = _session()
    parent = _call(MAIN, "spawn_team", task="audit", agent_names="a, b", mode="")
    await _collect(_resume(session).resume(_ready(_link(parent))))
    given = _given(session)
    assert "The team could not be rebuilt: no spawn tools in this session." in given
    # A team spawned with no mode is a coordinated team, as a live spawn says.
    assert "[Team: a, b] (mode: coordinate)" in given


@pytest.mark.asyncio
async def test_a_team_whose_member_left_the_pool_is_reported_to_the_waiting_call():
    pool = EmptyPool()
    tools = OrchestrateTools(pool=pool, settings=Settings(), session_id="sess")
    session = _session([tools])
    session.pool = pool
    parent = _call(MAIN, "spawn_team", task="audit", agent_names="a, b", mode="route")
    await _collect(_resume(session).resume(_ready(_link(parent))))
    given = _given(session)
    assert "The team could not be rebuilt: No agent named 'a'." in given
    assert "(mode: route)" in given


def test_a_sub_agent_spawn_is_headed_by_its_agent_not_a_team():
    call = _call(MAIN, "spawn_agent", task="clean", agent_name="cleaner")
    assert ColdSpawnResume.tool_result(call, "done") == (
        "[Agent: cleaner]\n[Task: clean]\n\nResponse:\ndone"
    )
