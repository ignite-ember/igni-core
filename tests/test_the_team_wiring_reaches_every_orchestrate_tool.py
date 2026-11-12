"""``TeamWiring`` puts the window's callbacks and the queue hooks on the session's team.

It reads ``session.main_team`` at call time, so a team rebuilt after a model
switch is wired afresh, and it touches only the orchestrate toolkits. The
toolkits and the queue bridge are real; the session and the team are the bare
objects the wiring reads (``tools``, ``tool_hooks``, ``post_hooks``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from igni_core.backend.team_wiring import TeamWiring
from igni_core.core.hooks.queue import QueueInjectorHook, QueuePersisterHook
from igni_core.core.tools.orchestrate import OrchestrateTools
from igni_core.core.tools.orchestrate_registry import SubAgentRegistry


def _toolkit() -> OrchestrateTools:
    settings = MagicMock()
    settings.orchestration.max_nesting_depth = 5
    settings.orchestration.max_total_agents = 20
    return OrchestrateTools(pool=MagicMock(), settings=settings)


def _session(*tools: object, tool_hooks=None, post_hooks=None) -> SimpleNamespace:
    team = SimpleNamespace(tools=list(tools), tool_hooks=tool_hooks, post_hooks=post_hooks)
    return SimpleNamespace(main_team=team)


def test_the_queue_hook_registers_an_injector_and_a_persister_on_the_current_team() -> None:
    existing_tool, existing_post = object(), object()
    session = _session(tool_hooks=[existing_tool], post_hooks=[existing_post])

    TeamWiring(session).wire_queue_hook([])  # type: ignore[arg-type]

    team = session.main_team
    assert team.tool_hooks[0] is existing_tool
    assert isinstance(team.tool_hooks[-1], QueueInjectorHook)
    assert team.post_hooks[0] is existing_post
    assert isinstance(team.post_hooks[-1], QueuePersisterHook)


async def test_text_queued_for_the_run_is_added_to_the_next_tool_result_and_then_persisted() -> (
    None
):
    queue = ["look at foo.py too"]
    session = _session()
    TeamWiring(session).wire_queue_hook(queue)  # type: ignore[arg-type]
    injector = session.main_team.tool_hooks[-1]
    persister = session.main_team.post_hooks[-1]

    result = await injector(name="read", func=lambda: "file body", args={})

    assert queue == []
    assert result.startswith("file body")
    assert "look at foo.py too" in result

    run_output = SimpleNamespace(messages=[])
    persister(run_output=run_output)
    assert [m.role for m in run_output.messages] == ["user"]
    assert "look at foo.py too" in str(run_output.messages[0].content)


def test_progress_and_the_event_appender_reach_every_orchestrate_tool() -> None:
    first, second = _toolkit(), _toolkit()
    other = object()
    session = _session(first, other, second)
    wiring = TeamWiring(session)

    def progress(*args: object) -> None: ...

    def appender(*args: object) -> None: ...

    wiring.wire_orchestrate_progress(progress)  # type: ignore[arg-type]
    wiring.wire_orchestrate_event_appender(appender)  # type: ignore[arg-type]

    for tool in (first, second):
        assert tool.wiring.sinks.on_progress is progress
        assert tool.wiring.sinks.event_appender is appender


def test_a_team_without_tools_wires_nothing_and_counts_zero() -> None:
    for tools in (None, []):
        session = SimpleNamespace(main_team=SimpleNamespace(tools=tools))
        wiring = TeamWiring(session)  # type: ignore[arg-type]
        wiring.wire_orchestrate_progress(lambda *a: None)  # type: ignore[arg-type]
        wiring.wire_orchestrate_event_appender(lambda *a: None)  # type: ignore[arg-type]
        assert wiring.active_subagent_runs() == 0


def test_runs_in_flight_are_summed_and_a_shared_registry_counts_once() -> None:
    shared = SubAgentRegistry({"r1", "r2"})
    a, b, c = _toolkit(), _toolkit(), _toolkit()
    a.wiring.sinks.subagent_registry = shared
    b.wiring.sinks.subagent_registry = shared
    c.wiring.sinks.subagent_registry = SubAgentRegistry({"r3"})

    assert TeamWiring(_session(a, b)).active_subagent_runs() == 2  # type: ignore[arg-type]
    assert TeamWiring(_session(a, b, c, object())).active_subagent_runs() == 3  # type: ignore[arg-type]


def test_a_rebuilt_team_is_wired_not_the_stale_one() -> None:
    old_tool, new_tool = _toolkit(), _toolkit()
    session = _session(old_tool)
    wiring = TeamWiring(session)  # type: ignore[arg-type]

    session.main_team = SimpleNamespace(tools=[new_tool], tool_hooks=None, post_hooks=None)

    def progress(*args: object) -> None: ...

    wiring.wire_orchestrate_progress(progress)  # type: ignore[arg-type]

    assert new_tool.wiring.sinks.on_progress is progress
    assert old_tool.wiring.sinks.on_progress is None
