"""A team's stream shows its members, never its coordinator.

``TeamStreamHandler`` is fed Agno events and answers with progress
events, activity-log lines and, on a pause, a continued stream. The
team's own coordinator events are absent from the dispatch table, and
the team's run id is read from team-level events only.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from agno.models.response import ToolExecution
from agno.run import agent as agent_events
from agno.run import team as team_events

from igni_core.core.tools.orchestrate_events import (
    AgentCompletedEvent,
    AgentPausedEvent,
    AgentStartedEvent,
    ContentPreviewEvent,
    RunErrorEvent,
    TaskCreatedEvent,
    TaskUpdatedEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
)
from igni_core.core.tools.orchestrate_stream_context import StreamContext, StreamSinks
from igni_core.core.tools.orchestrate_team_stream import TEAM_EVENT_HANDLERS, TeamStreamHandler


class _Coordinator:
    def __init__(self, wait_error: BaseException | None = None) -> None:
        self.pushed: list[dict[str, Any]] = []
        self.waited: list[str] = []
        self.cleaned: list[str] = []
        self.wait_error = wait_error

    async def push_requirement(self, req, *, run_id, agent_path, session_id):
        self.pushed.append(
            {"req": req, "run_id": run_id, "agent_path": agent_path, "session_id": session_id}
        )
        return f"req-{len(self.pushed)}"

    async def wait_resolved(self, req_id: str) -> None:
        self.waited.append(req_id)
        if self.wait_error is not None:
            raise self.wait_error

    def cleanup(self, req_id: str) -> None:
        self.cleaned.append(req_id)

    def forget_spawn(self, run_id: str) -> None:
        pass


class _Team:
    def __init__(self) -> None:
        self.continued: list[dict[str, Any]] = []

    def acontinue_run(self, **kwargs: Any) -> str:
        self.continued.append(kwargs)
        return "follow-up-stream"


def _handler(
    path: list[str] | None = None, coordinator: Any = None
) -> tuple[TeamStreamHandler, list[Any], _Team]:
    emitted: list[Any] = []
    team = _Team()
    handler = TeamStreamHandler(
        StreamContext(
            runnable=team,
            task="review it",
            agent_path=path or [],
            card_id="card-1",
            sinks=StreamSinks(on_progress=emitted.append, hitl_coordinator=coordinator),
        )
    )
    return handler, emitted, team


def _req(member: str | None = None) -> Any:
    return SimpleNamespace(is_resolved=lambda: False, member_agent_name=member)


def _tool(name: str | None = "grep", args: dict | None = None, result: Any = None) -> Any:
    return ToolExecution(tool_call_id="tc-1", tool_name=name, tool_args=args or {}, result=result)


# ── The table and the paths ────────────────────────────────────────


def test_the_table_answers_members_and_team_tasks_but_not_the_coordinator():
    assert team_events.RunStartedEvent not in TEAM_EVENT_HANDLERS
    assert team_events.RunCompletedEvent not in TEAM_EVENT_HANDLERS
    assert team_events.ToolCallStartedEvent not in TEAM_EVENT_HANDLERS
    assert team_events.ToolCallCompletedEvent not in TEAM_EVENT_HANDLERS
    assert team_events.RunContentEvent not in TEAM_EVENT_HANDLERS
    assert set(TEAM_EVENT_HANDLERS) == {
        agent_events.RunStartedEvent,
        agent_events.ToolCallStartedEvent,
        agent_events.ToolCallCompletedEvent,
        agent_events.ToolCallErrorEvent,
        agent_events.RunErrorEvent,
        agent_events.RunCompletedEvent,
        agent_events.RunContentEvent,
        team_events.RunPausedEvent,
        team_events.TaskCreatedEvent,
        team_events.TaskUpdatedEvent,
        team_events.TaskIterationStartedEvent,
    }
    for method in TEAM_EVENT_HANDLERS.values():
        assert callable(getattr(TeamStreamHandler, method))


@pytest.mark.parametrize(
    ("base", "team_path"), [([], "team"), (["architect"], "architect"), (["a", "b"], "a.b")]
)
def test_the_team_is_rooted_at_the_context_path(base, team_path):
    handler, _, _ = _handler(base)

    assert handler.state.team_path_id == team_path
    assert handler.state.card_id == "card-1"
    assert handler._agent_path_for(None) == team_path
    assert handler._agent_path_for("") == team_path
    assert handler._agent_path_for("sec") == (".".join([*base, "sec"]))


async def test_an_unmapped_event_is_ignored_even_when_it_carries_ids():
    handler, emitted, _ = _handler()

    result = await handler._handle(
        team_events.ToolCallStartedEvent(team_name="lead", tool=_tool(), run_id="t-run")
    )

    assert result is None
    assert emitted == []
    assert handler.state.log == []


async def test_the_team_run_is_latched_from_team_events_only():
    handler, _, _ = _handler()

    await handler._handle(
        agent_events.RunStartedEvent(agent_name="sec", run_id="member-run", session_id="m-sess")
    )
    assert handler.state.current_run_id in (None, "")
    assert not handler.state.current_session_id

    await handler._handle(
        team_events.RunStartedEvent(team_name="lead", run_id="team-run", session_id="t-sess")
    )
    assert handler.state.current_run_id == "team-run"
    assert handler.state.current_session_id == "t-sess"

    await handler._handle(agent_events.RunStartedEvent(agent_name="perf", run_id="member-2"))
    assert handler.state.current_run_id == "team-run"


async def test_a_nested_team_member_is_named_by_its_team_name():
    handler, emitted, _ = _handler()
    nested = agent_events.RunStartedEvent(agent_name=None, run_id="n-1")
    nested.team_name = "sub-team"

    await handler._handle(nested)

    assert [(e.agent, e.agent_path) for e in emitted] == [("sub-team", "sub-team")]


# ── Members ────────────────────────────────────────────────────────


async def test_a_new_member_is_announced_once_with_its_run_and_the_task():
    handler, emitted, _ = _handler(["architect"])

    for _ in range(2):
        await handler._handle(agent_events.RunStartedEvent(agent_name="sec", run_id="m-1"))
    await handler._handle(agent_events.RunStartedEvent(agent_name=None, run_id="m-x"))

    assert emitted == [
        AgentStartedEvent(
            agent_path="architect.sec",
            agent="sec",
            parent="architect",
            run_id="m-1",
            task="review it",
            card_id="card-1",
        )
    ]
    assert handler.state.log == ["  ├─ [sec]"]
    assert handler.state.current_agent == "sec"


async def test_interleaved_tool_events_land_on_the_member_that_emitted_them():
    handler, emitted, _ = _handler()
    await handler._handle(agent_events.RunStartedEvent(agent_name="a"))
    await handler._handle(agent_events.RunStartedEvent(agent_name="b"))
    emitted.clear()

    await handler._handle(
        agent_events.ToolCallStartedEvent(agent_name="a", tool=_tool("grep", {"q": "x"}))
    )
    await handler._handle(agent_events.ToolCallStartedEvent(tool=_tool("ls")))

    assert [(e.agent_path, e.tool) for e in emitted] == [("a", "grep"), ("b", "ls")]
    assert emitted[0] == ToolStartedEvent(
        agent_path="a", tool="grep", tool_call_id="tc-1", args="q=x", card_id="card-1"
    )
    assert handler.state.log[-2:] == ["  │  ├─ grep(q=x)", "  │  ├─ ls()"]


async def test_an_event_naming_no_member_falls_back_to_the_team_path():
    handler, emitted, _ = _handler(["architect"])

    await handler._handle(agent_events.ToolCallStartedEvent(tool=None))

    assert emitted[0].agent_path == "architect"
    assert emitted[0].tool == "tool"
    assert emitted[0].args == ""


async def test_a_tool_completing_reports_its_result_and_clears_the_current_tool():
    handler, emitted, _ = _handler()
    await handler._handle(agent_events.ToolCallStartedEvent(agent_name="a", tool=_tool("grep")))
    assert handler.state.current_tool == "grep"
    emitted.clear()

    await handler._handle(
        agent_events.ToolCallCompletedEvent(agent_name="a", tool=_tool("grep", result="found\nit"))
    )

    assert emitted == [
        ToolCompletedEvent(
            agent_path="a",
            tool="grep",
            tool_call_id="tc-1",
            result="found it",
            is_error=False,
            card_id="card-1",
        )
    ]
    assert handler.state.current_tool is None
    assert handler.state.log[-1] == "  │  │  └─ found\nit"


async def test_a_completed_tool_without_a_name_or_result_uses_the_current_tool():
    handler, emitted, _ = _handler()
    handler.state.current_tool = "grep"

    await handler._handle(
        agent_events.ToolCallCompletedEvent(agent_name="a", tool=_tool(None, result=None))
    )
    assert emitted[0].tool == "grep"
    assert emitted[0].result == ""
    assert handler.state.log[-1] == "  │  │  └─ "

    await handler._handle(agent_events.ToolCallCompletedEvent(agent_name="a", tool=None))
    assert emitted[1].tool == "tool"
    assert emitted[1].tool_call_id is None


async def test_a_tool_error_is_clipped_to_two_hundred_and_flagged():
    handler, emitted, _ = _handler()
    handler.state.current_tool = "grep"
    long = "x" * 500

    await handler._handle(
        agent_events.ToolCallErrorEvent(agent_name="a", tool=_tool("grep"), error=long)
    )

    assert emitted == [
        ToolCompletedEvent(
            agent_path="a",
            tool="grep",
            tool_call_id="tc-1",
            result="x" * 200,
            is_error=True,
            card_id="card-1",
        )
    ]
    assert handler.state.current_tool is None
    assert handler.state.log[-1] == f"  │  │  └─ ERROR: {long}"


async def test_a_tool_error_for_another_tool_keeps_the_current_one():
    handler, _, _ = _handler()
    handler.state.current_tool = "ls"

    await handler._handle(
        agent_events.ToolCallErrorEvent(agent_name="a", tool=_tool("grep"), error="no")
    )

    assert handler.state.current_tool == "ls"


async def test_a_run_error_is_clipped_to_four_hundred():
    handler, emitted, _ = _handler()

    await handler._handle(agent_events.RunErrorEvent(agent_name="a", content="e" * 900))

    assert emitted == [RunErrorEvent(agent_path="a", error="e" * 400, card_id="card-1")]
    assert handler.state.log[-1] == "  │  └─ ERROR: " + "e" * 900


async def test_a_member_completing_reports_its_token_totals():
    handler, emitted, _ = _handler()
    metrics = SimpleNamespace(input_tokens=11, output_tokens=7, reasoning_tokens=3)

    await handler._handle(
        agent_events.RunCompletedEvent(agent_name="a", content="done", metrics=metrics)
    )

    assert emitted == [
        AgentCompletedEvent(
            agent_path="a",
            is_error=False,
            input_tokens=11,
            output_tokens=7,
            reasoning_tokens=3,
            card_id="card-1",
        )
    ]
    assert handler.state.completed_content == "done"


async def test_missing_metrics_or_token_fields_count_as_zero():
    handler, emitted, _ = _handler()
    partial = SimpleNamespace(input_tokens=None, output_tokens=5)

    await handler._handle(agent_events.RunCompletedEvent(agent_name="a", metrics=None))
    await handler._handle(agent_events.RunCompletedEvent(agent_name="b", metrics=partial))

    assert [(e.input_tokens, e.output_tokens, e.reasoning_tokens) for e in emitted] == [
        (0, 0, 0),
        (0, 5, 0),
    ]


async def test_member_content_is_previewed_once_then_throttled():
    handler, emitted, _ = _handler()

    await handler._handle(agent_events.RunContentEvent(agent_name="a", content=""))
    await handler._handle(agent_events.RunContentEvent(agent_name="a", content="hello"))
    await handler._handle(agent_events.RunContentEvent(agent_name="a", content=" world"))
    await handler._handle(agent_events.RunContentEvent(agent_name="b", content="other"))

    assert [(type(e), e.agent_path) for e in emitted] == [
        (ContentPreviewEvent, "a"),
        (ContentPreviewEvent, "b"),
    ]
    assert "hello" in emitted[0].text
    assert "other" in emitted[1].text


# ── Tasks ──────────────────────────────────────────────────────────


async def test_a_task_is_logged_and_emitted_with_its_assignee():
    handler, emitted, _ = _handler()

    await handler._handle(team_events.TaskCreatedEvent(title="audit", assignee="sec"))
    await handler._handle(team_events.TaskCreatedEvent(title="loose", assignee=None))

    assert emitted == [
        TaskCreatedEvent(agent_path="sec", title="audit", assignee="sec", card_id="card-1"),
        TaskCreatedEvent(agent_path="team", title="loose", assignee="", card_id="card-1"),
    ]
    assert handler.state.log == [
        "  ┌─ TASK: audit",
        "  │  assigned to: sec",
        "  ┌─ TASK: loose",
    ]


@pytest.mark.parametrize(
    ("status", "icon"),
    [("completed", "✓"), ("failed", "✗"), ("running", "…"), ("weird", "·"), (None, "·")],
)
async def test_a_task_update_shows_its_status_with_an_icon(status, icon):
    handler, emitted, _ = _handler()
    handler.state.current_agent = "sec"

    await handler._handle(team_events.TaskUpdatedEvent(status=status))

    assert emitted == [TaskUpdatedEvent(agent_path="sec", status=status or "", card_id="card-1")]
    assert handler.state.log == [f"  │  {icon} {status or ''}"]


async def test_a_task_iteration_is_only_logged():
    handler, emitted, _ = _handler()

    await handler._handle(team_events.TaskIterationStartedEvent(iteration=3))

    assert emitted == []
    assert handler.state.log == ["  ╞═ Iteration 3"]


# ── Pause ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("with_coordinator", [False, True])
async def test_a_pause_with_nothing_to_ask_says_there_is_no_bridge(with_coordinator):
    coordinator = _Coordinator() if with_coordinator else None
    handler, emitted, team = _handler(coordinator=coordinator)
    reqs = [] if with_coordinator else [_req()]

    result = await handler._handle(team_events.RunPausedEvent(requirements=reqs))

    assert result is None
    assert emitted == []
    assert team.continued == []
    assert handler.state.log == ["  │  ⚠ paused: no HITL bridge available"]
    if coordinator:
        assert coordinator.pushed == []


async def test_a_pause_asks_for_each_decision_then_continues_the_team_run():
    coordinator = _Coordinator()
    handler, emitted, team = _handler(["architect"], coordinator)
    handler.state.current_agent = "sec"
    reqs = [_req("sec"), _req(None)]

    result = await handler._handle(
        team_events.RunPausedEvent(requirements=reqs, run_id="t-run", session_id="t-sess")
    )

    assert result == "follow-up-stream"
    assert [(p["agent_path"], p["run_id"], p["session_id"]) for p in coordinator.pushed] == [
        (["architect", "sec"], "t-run", "t-sess"),
        (["architect"], "t-run", "t-sess"),
    ]
    assert coordinator.waited == ["req-1", "req-2"]
    assert coordinator.cleaned == ["req-1", "req-2"]
    assert emitted == [AgentPausedEvent(agent_path="architect.sec", count=2, card_id="card-1")]
    assert handler.state.log == ["  │  ⏸ awaiting approval (2 tools)"]
    assert team.continued == [
        {"run_id": "t-run", "session_id": "t-sess", "requirements": reqs, "stream": True}
    ]


async def test_a_single_requirement_pause_logs_without_a_count():
    coordinator = _Coordinator()
    handler, _, _ = _handler(coordinator=coordinator)

    await handler._handle(team_events.RunPausedEvent(requirements=[_req()], run_id="r"))

    assert handler.state.log == ["  │  ⏸ awaiting approval"]


async def test_a_pause_without_ids_uses_the_latched_ones():
    coordinator = _Coordinator()
    handler, _, team = _handler(coordinator=coordinator)
    handler.state.current_run_id = "latched-run"
    handler.state.current_session_id = "latched-sess"

    await handler._handle(team_events.RunPausedEvent(requirements=[_req()]))

    assert team.continued[0]["run_id"] == "latched-run"
    assert team.continued[0]["session_id"] == "latched-sess"


@pytest.mark.parametrize("error", [RuntimeError("boom"), asyncio.CancelledError()])
async def test_a_wait_that_fails_still_cleans_up_every_requirement(error):
    coordinator = _Coordinator(wait_error=error)
    handler, _, team = _handler(coordinator=coordinator)

    with pytest.raises(type(error)):
        await handler._handle(team_events.RunPausedEvent(requirements=[_req(), _req()], run_id="r"))

    assert coordinator.cleaned == ["req-1", "req-2"]
    assert team.continued == []
