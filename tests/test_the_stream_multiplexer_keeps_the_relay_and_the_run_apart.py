"""The multiplexer's relay is open before the run starts, fails loudly, and is handed over whole.

``test_a_run_and_its_sub_agents_questions_reach_the_ui_together`` pins
what reaches the window. These pin the relay around it: it counts as
open before the team's first step (a tool that asks at once is not
refused as "nobody to ask"), a coordinator that fails ends the stream in
an ``Error``, a nested resume gets back the explanations as well as the
questions, the run's inline ``<think>`` state ends with the run, and an
evaluator decision with no run to resume is not resumed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from agno.models.response import ToolExecution
from agno.run.agent import RunCompletedEvent, RunContentEvent, RunOutput, RunPausedEvent
from agno.run.requirement import RunRequirement
from agno.run.team import RunContentEvent as TeamContentEvent
from agno.run.team import TeamRunOutput

from igni_core.backend.hitl_stream_mux import HITLStreamMultiplexer, _run_id_of
from igni_core.backend.hitl_tracer import HITLTracer
from igni_core.backend.pause_handler import PauseHandler
from igni_core.backend.pending_requirements_store import PendingRequirementsStore
from igni_core.backend.schemas_pause import SubagentDetails, TeamStreamEvent
from igni_core.core.config.permission_eval import PermissionEvaluator, PermissionMode
from igni_core.core.hitl.models import DetailUpdate
from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator
from igni_core.protocol import messages as msg


class Team:
    def __init__(self) -> None:
        self.continued: list[dict[str, Any]] = []

    async def acontinue_run(self, **kwargs: Any) -> AsyncIterator[Any]:
        self.continued.append(kwargs)
        yield RunCompletedEvent(run_id=kwargs["run_id"])


def _mux(
    coordinator: SubAgentHITLCoordinator | None = None, evaluator: PermissionEvaluator | None = None
) -> tuple[HITLStreamMultiplexer, SimpleNamespace]:
    store = PendingRequirementsStore()
    session = SimpleNamespace(
        sub_agent_hitl=coordinator or SubAgentHITLCoordinator(),
        broadcast_bus=SimpleNamespace(drain_post_run=lambda run_id: None),
        main_team=Team(),
        session_id="s-1",
    )
    mux = HITLStreamMultiplexer(
        session=session,  # type: ignore[arg-type]
        store=store,
        pause_handler=PauseHandler(evaluator=evaluator, store=store),
        tracer=HITLTracer(enabled=False),
    )
    return mux, session


async def _collect(stream: AsyncIterator[msg.Message]) -> list[msg.Message]:
    return [m async for m in stream]


async def _events(*events: Any) -> AsyncIterator[Any]:
    for event in events:
        yield event


# ── The relay around a run ────────────────────────────────────────


async def test_the_relay_is_open_at_the_team_s_first_step_and_closed_after():
    mux, session = _mux()
    seen: list[bool] = []

    async def team() -> AsyncIterator[Any]:
        # The first thing a fast tool call does: look for someone to ask.
        seen.append(session.sub_agent_hitl.has_relay)
        yield RunCompletedEvent(run_id="r")

    await _collect(mux.stream(team()))

    assert seen == [True]
    assert not session.sub_agent_hitl.has_relay


async def test_the_relay_is_closed_when_the_stream_is_abandoned_midway():
    mux, session = _mux()
    release = asyncio.Event()

    async def team() -> AsyncIterator[Any]:
        yield RunContentEvent(content="first", run_id="r")
        await release.wait()
        yield RunCompletedEvent(run_id="r")

    stream = mux.stream(team())
    first = await anext(stream)
    assert first.type == "content_delta" and session.sub_agent_hitl.has_relay
    await stream.aclose()
    release.set()

    assert not session.sub_agent_hitl.has_relay


class FailingCoordinator(SubAgentHITLCoordinator):
    def list_new_pending(self) -> Any:
        raise RuntimeError("the journal is gone")


async def test_a_coordinator_that_fails_ends_the_stream_in_an_error_not_a_silent_relay():
    coordinator = FailingCoordinator()
    mux, session = _mux(coordinator)
    release = asyncio.Event()

    async def team() -> AsyncIterator[Any]:
        coordinator.new_arrival.set()
        await release.wait()
        yield RunCompletedEvent(run_id="r")

    try:
        out = await asyncio.wait_for(_collect(mux.stream(team())), 2)
    finally:
        release.set()

    assert [(m.type, m.text) for m in out] == [("error", "the journal is gone")]
    assert not session.sub_agent_hitl.has_relay


# ── Handing the relay to a nested resume ──────────────────────────


async def test_a_nested_resume_gets_the_explanations_back_and_the_other_events_stay_in_order():
    coordinator = SubAgentHITLCoordinator()
    mux, _ = _mux(coordinator)
    requirement = RunRequirement(
        tool_execution=ToolExecution(
            tool_name="save_file", tool_args={"file_name": "a.py"}, requires_confirmation=True
        )
    )
    asking = asyncio.create_task(coordinator.push_requirement(requirement, run_id="sub-1"))
    for _ in range(100):
        shown = coordinator.list_new_pending()
        if shown:
            break
        await asyncio.sleep(0.01)
    (handle,) = shown
    coordinator.new_arrival.clear()
    first, last = TeamStreamEvent(event="first"), TeamStreamEvent(event="last")
    queue: asyncio.Queue = asyncio.Queue()
    for event in (
        first,
        SubagentDetails(
            updates=[DetailUpdate(requirement_id=handle.requirement_id, details="it overwrites")]
        ),
        last,
    ):
        queue.put_nowait(event)
    relay = asyncio.create_task(asyncio.sleep(3600))
    mux._sub_task, mux._queue = relay, queue

    mux._hand_relay_to_nested()
    await asyncio.sleep(0)

    assert relay.cancelled()
    assert [queue.get_nowait(), queue.get_nowait()] == [first, last] and queue.empty()
    (update,) = coordinator.take_detail_updates()
    assert (update.requirement_id, update.details) == (handle.requirement_id, "it overwrites")
    assert coordinator.new_arrival.is_set()
    asking.cancel()


async def test_a_question_asked_inside_an_auto_resumed_run_reaches_the_window():
    """In Bypass every shell call is auto-resumed inside the stream's own loop, and
    that run's tool asks the person. The outer relay used to wake first, take the
    question into a queue nobody read until the resumed run ended, and the run was
    waiting on the answer: a deadlock with no dialog."""
    coordinator = SubAgentHITLCoordinator()
    evaluator = PermissionEvaluator.from_strings(mode=PermissionMode.BYPASS_PERMISSIONS)
    mux, session = _mux(coordinator, evaluator)
    answered = asyncio.Event()
    guard = RunRequirement(
        tool_execution=ToolExecution(
            tool_name="run_shell_command", tool_args={"command": "make"}, requires_confirmation=True
        )
    )
    inside = RunRequirement(
        tool_execution=ToolExecution(
            tool_name="save_file", tool_args={"file_name": "a.py"}, requires_confirmation=True
        )
    )
    asked: list[asyncio.Task[str]] = []

    async def acontinue_run(**kwargs: Any) -> AsyncIterator[Any]:
        asked.append(asyncio.create_task(coordinator.push_requirement(inside, run_id="guard")))
        await answered.wait()
        yield RunCompletedEvent(run_id=kwargs["run_id"])

    session.main_team.acontinue_run = acontinue_run

    async def consume() -> list[msg.Message]:
        out: list[msg.Message] = []
        async for m in mux.stream(_events(RunPausedEvent(run_id="r", requirements=[guard]))):
            out.append(m)
            if isinstance(m, msg.RunPaused):
                coordinator.resolve(m.requirements[0].requirement_id, "confirm")
                answered.set()
        return out

    out = await asyncio.wait_for(consume(), 2)

    assert [m.type for m in out] == ["run_paused", "run_completed"]
    assert await asyncio.wait_for(asked[0], 1)


async def test_handing_over_with_nothing_queued_leaves_the_coordinator_alone():
    coordinator = SubAgentHITLCoordinator()
    mux, _ = _mux(coordinator)
    queue: asyncio.Queue = asyncio.Queue()
    mux._sub_task, mux._queue = asyncio.create_task(asyncio.sleep(3600)), queue

    mux._hand_relay_to_nested()
    await asyncio.sleep(0)

    assert queue.empty() and not coordinator.new_arrival.is_set()


# ── Which run an event belongs to ─────────────────────────────────


def test_a_run_id_is_read_off_agent_and_team_events_and_outputs_only():
    assert _run_id_of(RunContentEvent(run_id="a")) == "a"
    assert _run_id_of(TeamContentEvent(run_id="t")) == "t"
    assert _run_id_of(RunOutput(run_id="o")) == "o"
    assert _run_id_of(TeamRunOutput(run_id="to")) == "to"
    assert _run_id_of(msg.Info(text="x")) is None
    assert _run_id_of("not an event") is None
    assert _run_id_of(None) is None


# ── The run's own state ends with it ──────────────────────────────


async def test_an_unclosed_think_block_does_not_leak_past_the_end_of_its_run():
    mux, _ = _mux()

    out = await _collect(
        mux.stream(
            _events(
                RunContentEvent(content="<think>still pondering", run_id="r"),
                RunCompletedEvent(run_id="r"),
                RunContentEvent(content="the answer", run_id="r"),
            )
        )
    )

    contents = [m for m in out if isinstance(m, msg.ContentDelta)]
    assert [(m.text, m.is_thinking) for m in contents] == [
        ("still pondering", True),
        ("the answer", False),
    ]


async def test_a_decision_by_the_policy_with_no_run_to_resume_ends_quietly():
    evaluator = PermissionEvaluator.from_strings(mode=PermissionMode.BYPASS_PERMISSIONS)
    mux, session = _mux(evaluator=evaluator)
    req = RunRequirement(
        tool_execution=ToolExecution(
            tool_name="run_shell_command", tool_args={"command": "ls"}, requires_confirmation=True
        )
    )

    out = await _collect(mux.stream(_events(RunPausedEvent(run_id=None, requirements=[req]))))

    assert out == []
    assert session.main_team.continued == []
