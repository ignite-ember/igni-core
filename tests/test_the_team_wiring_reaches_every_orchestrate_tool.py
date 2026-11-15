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
    return SimpleNamespace(main_team=team, main_team_wirings=[])


def _rebuild(session: SimpleNamespace, *tools: object) -> None:
    """What ``Session.rebuild_main_team`` does: a fresh team, then the wirings again."""
    session.main_team = SimpleNamespace(tools=list(tools), tool_hooks=None, post_hooks=None)
    for apply in session.main_team_wirings:
        apply()


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
        session = SimpleNamespace(main_team=SimpleNamespace(tools=tools), main_team_wirings=[])
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


def test_a_team_rebuilt_after_the_wiring_is_wired_again() -> None:
    """The Trust click rebuilds the team after the window wired it: the
    fresh team kept no progress callback, appender or queue hooks."""
    session = _session(_toolkit())
    wiring = TeamWiring(session)  # type: ignore[arg-type]

    def progress(*args: object) -> None: ...

    def appender(*args: object) -> None: ...

    wiring.wire_orchestrate_progress(progress)  # type: ignore[arg-type]
    wiring.wire_orchestrate_event_appender(appender)  # type: ignore[arg-type]
    wiring.wire_queue_hook([])  # type: ignore[arg-type]

    fresh = _toolkit()
    _rebuild(session, fresh)

    assert fresh.wiring.sinks.on_progress is progress
    assert fresh.wiring.sinks.event_appender is appender
    assert isinstance(session.main_team.tool_hooks[-1], QueueInjectorHook)
    assert isinstance(session.main_team.post_hooks[-1], QueuePersisterHook)


async def test_the_visualizer_card_is_kept_in_the_session_event_log() -> None:
    from igni_core.core.session.event_log_schema import VisualizationDeltaDraft
    from igni_core.core.tools.orchestrate_events import VisualizationDeltaEvent

    drafts: list[object] = []

    async def append_event(draft: object) -> None:
        drafts.append(draft)

    tool = _toolkit()
    session = _session(tool)
    session.append_event = append_event
    TeamWiring(session).wire_event_log()  # type: ignore[arg-type]

    event = VisualizationDeltaEvent(
        agent_path="visualizer", spec_id="s1", spec_json='{"root": "a"}', final=True
    )
    await tool.wiring.sinks.event_appender(event, "run-1")  # type: ignore[misc]

    assert len(drafts) == 1
    draft = drafts[0]
    assert isinstance(draft, VisualizationDeltaDraft)
    assert draft.run_id == "run-1"
    assert draft.payload.spec_id == "s1"
    assert draft.payload.spec_json == '{"root": "a"}'
    assert draft.payload.final is True


def test_session_rebuild_applies_what_was_wired_to_the_fresh_team(monkeypatch) -> None:
    from igni_core.core.session import core as session_core

    fresh = _toolkit()

    class _Builder:
        def __init__(self, session: SimpleNamespace) -> None:
            self._session = session

        def build_main_team(self) -> None:
            self._session.main_team = SimpleNamespace(
                tools=[fresh], tool_hooks=None, post_hooks=None
            )

    monkeypatch.setattr(session_core, "AgentsBuilder", _Builder)
    session = _session(_toolkit())
    session.settings = object()
    session.context_budget = SimpleNamespace(resize=lambda settings: None)

    def progress(*args: object) -> None: ...

    TeamWiring(session).wire_orchestrate_progress(progress)  # type: ignore[arg-type]
    session_core.Session.rebuild_main_team(session)  # type: ignore[arg-type]

    assert fresh.wiring.sinks.on_progress is progress
