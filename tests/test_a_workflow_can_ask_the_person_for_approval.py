"""A workflow's agent call can ask the person for approval.

A workflow started from the composer is an RPC that returns at once:
its agent calls happen later, with no chat run of their own to stream a
pause on. Nothing was relaying the session's questions to a person, so
the approval channel answered that nobody could answer, and every
workflow whose agent called a tool that needed approval stopped with
``agent call stopped to ask for approval of <tool>, and nobody can
answer`` (campaign R664).

What we pin:

* the question reaches the window as a ``run_paused`` carrying the tool
  and its arguments, the way the chat's own pauses do;
* the person's answer resumes the call — confirmed, the agent runs on;
  declined, the call stops with ``ok=false``;
* a session nobody can answer in (a headless run) is still refused the
  same way as before — the relay does not turn a headless run into one
  that waits for ever;
* the relay is given back when the call ends, and the chat's own relay
  is left alone.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from agno.models.response import ToolExecution
from agno.run.requirement import RunRequirement

from igni_core.backend.push_bridge import PushNotificationBridge
from igni_core.backend.schemas_workflow_events import AgentRequestEvent, AgentRequestPayload
from igni_core.backend.workflow_agent_bridge import WorkflowAgentBridge
from igni_core.backend.workflow_hitl import WorkflowApprovalRelay
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.session.tool_approval import ToolCallApprovalChannel
from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator
from igni_core.protocol import messages as msg


class _Transport:
    """What the windows would have received."""

    def __init__(self) -> None:
        self.sent: list[msg.Message] = []

    async def send(self, message: msg.Message) -> None:
        self.sent.append(message)

    async def receive(self):  # pragma: no cover — the push bridge never reads
        yield None


def _requirement(tool_name: str, tool_args: dict) -> RunRequirement:
    """One Agno requirement: the tool call the run paused on.

    A real ``RunRequirement``, because the pipeline reads the run's
    requirements through ``TurnOutcome``, which validates them.
    """
    return RunRequirement(
        tool_execution=ToolExecution(
            tool_name=tool_name,
            tool_args=tool_args,
            requires_confirmation=True,
        )
    )


class _Run:
    """What Agno hands back: a status, and the requirements it waits on."""

    def __init__(self, status: str, requirements: list[RunRequirement] | None = None) -> None:
        self.status = status
        self.content = ""
        self.metrics = None
        self.active_requirements = list(requirements or [])
        self.requirements = list(requirements or [])
        self.tools = []


def _request(payload: dict) -> AgentRequestEvent:
    return AgentRequestEvent(
        ts=1, run_id="wf_1", seq=0, payload=AgentRequestPayload.model_validate(payload)
    )


def _team(run: _Run, resumed: _Run) -> MagicMock:
    team = MagicMock()
    team.arun = AsyncMock(return_value=run)
    team.acontinue_run = AsyncMock(return_value=resumed)
    return team


def _session(team: MagicMock) -> MagicMock:
    session = MagicMock()
    session.session_id = "s-1"
    session.main_team = team
    # No permission policy: the pause is the person's to answer.
    session.permission_evaluator = None
    session.tool_audit = None
    session.sub_agent_hitl = SubAgentHITLCoordinator()
    session.approval = ToolCallApprovalChannel(session.sub_agent_hitl)
    return session


def _push() -> tuple[PushNotificationBridge, _Transport]:
    transport = _Transport()
    bridge = PushNotificationBridge(transport=transport, loop=asyncio.get_running_loop(), queue=[])
    return bridge, transport


async def _wait_for_pause(transport: _Transport) -> msg.RunPaused:
    """The pause the window was shown, once it is shown."""

    async def wait() -> msg.RunPaused:
        while True:
            for message in transport.sent:
                if isinstance(message, msg.RunPaused):
                    return message
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(wait(), timeout=5)


async def test_the_workflow_asks_the_person_and_their_answer_resumes_the_call() -> None:
    """The question reaches the window, and the answer lets the call go on."""
    requirement = _requirement("shell", {"cmd": "ls"})
    team = _team(_Run("paused", [requirement]), _Run("completed"))
    session = _session(team)
    push, transport = _push()
    bridge = WorkflowAgentBridge(session, RunGate(), WorkflowApprovalRelay(session, push))

    call = asyncio.create_task(bridge.answer(_request({"id": "r1", "prompt": "p"})))

    paused = await _wait_for_pause(transport)
    assert len(paused.requirements) == 1
    assert paused.requirements[0].tool_name == "shell"
    assert paused.requirements[0].tool_args == {"cmd": "ls"}
    # The dialog says what it is about.
    assert "shell" in paused.requirements[0].details

    session.sub_agent_hitl.resolve(paused.requirements[0].requirement_id, "confirm")

    reply = await asyncio.wait_for(call, timeout=5)
    assert reply.ok is True
    # The paused call was confirmed, and the run continued.
    assert requirement.tool_execution.confirmed is True
    team.acontinue_run.assert_awaited_once()


async def test_a_declined_question_stops_the_call_with_ok_false() -> None:
    """A refusal is a refusal: the call stops, and says so."""
    requirement = _requirement("shell", {"cmd": "rm -rf /"})
    team = _team(_Run("paused", [requirement]), _Run("completed"))
    session = _session(team)
    push, transport = _push()
    bridge = WorkflowAgentBridge(session, RunGate(), WorkflowApprovalRelay(session, push))

    call = asyncio.create_task(bridge.answer(_request({"id": "r2", "prompt": "p"})))

    paused = await _wait_for_pause(transport)
    session.sub_agent_hitl.resolve(paused.requirements[0].requirement_id, "reject")

    reply = await asyncio.wait_for(call, timeout=5)
    assert reply.ok is False
    assert reply.error == "agent call stopped: shell was declined"
    team.acontinue_run.assert_not_awaited()


async def test_a_call_with_nobody_to_ask_is_still_refused_the_same_way() -> None:
    """Without a relay there is nobody to answer, and the call says so.

    This is what every workflow's agent call used to get (campaign
    R664); a bridge with no relay at all still behaves that way.
    """
    requirement = _requirement("shell", {"cmd": "ls"})
    team = _team(_Run("paused", [requirement]), _Run("completed"))
    session = _session(team)

    reply = await WorkflowAgentBridge(session, RunGate()).answer(
        _request({"id": "r3", "prompt": "p"})
    )

    assert reply.ok is False
    assert reply.error == "agent call stopped to ask for approval of shell, and nobody can answer"
    team.acontinue_run.assert_not_awaited()


async def test_a_headless_run_is_not_left_waiting_for_an_answer() -> None:
    """Nobody at a terminal: the question is not asked of the air.

    A headless run marks its session unattended. The relay does not
    open for it, so the call is refused at once instead of waiting for
    an answer that can never come.
    """
    requirement = _requirement("shell", {"cmd": "ls"})
    team = _team(_Run("paused", [requirement]), _Run("completed"))
    session = _session(team)
    session.sub_agent_hitl.mark_unattended()
    push, transport = _push()
    bridge = WorkflowAgentBridge(session, RunGate(), WorkflowApprovalRelay(session, push))

    reply = await asyncio.wait_for(bridge.answer(_request({"id": "r4", "prompt": "p"})), timeout=5)

    assert reply.ok is False
    assert reply.error == "agent call stopped to ask for approval of shell, and nobody can answer"
    assert transport.sent == []
    assert session.sub_agent_hitl.has_relay is False


async def test_the_relay_is_given_back_when_the_call_ends() -> None:
    """The session's relay is a count: the chat's own share stays."""
    requirement = _requirement("shell", {"cmd": "ls"})
    team = _team(_Run("paused", [requirement]), _Run("completed"))
    session = _session(team)
    push, transport = _push()
    # The chat's own multiplexer is relaying too.
    session.sub_agent_hitl.open_relay()
    bridge = WorkflowAgentBridge(session, RunGate(), WorkflowApprovalRelay(session, push))

    call = asyncio.create_task(bridge.answer(_request({"id": "r5", "prompt": "p"})))
    paused = await _wait_for_pause(transport)
    assert session.sub_agent_hitl.has_relay is True

    session.sub_agent_hitl.resolve(paused.requirements[0].requirement_id, "confirm")
    await asyncio.wait_for(call, timeout=5)

    # The chat's relay is still open; the workflow gave back its own.
    assert session.sub_agent_hitl.has_relay is True
    session.sub_agent_hitl.close_relay()
    assert session.sub_agent_hitl.has_relay is False


@pytest.mark.parametrize("action", ["confirm", "reject"])
async def test_the_answer_the_person_gives_is_the_one_that_counts(action: str) -> None:
    """Whichever way the person answers, the call follows it."""
    requirement = _requirement("shell", {"cmd": "ls"})
    team = _team(_Run("paused", [requirement]), _Run("completed"))
    session = _session(team)
    push, transport = _push()
    bridge = WorkflowAgentBridge(session, RunGate(), WorkflowApprovalRelay(session, push))

    call = asyncio.create_task(bridge.answer(_request({"id": "r6", "prompt": "p"})))
    paused = await _wait_for_pause(transport)
    session.sub_agent_hitl.resolve(paused.requirements[0].requirement_id, action)
    reply = await asyncio.wait_for(call, timeout=5)

    assert reply.ok is (action == "confirm")
