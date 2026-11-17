"""A workflow's parallel lanes keep the relay until the last one is done.

A workflow's lanes run at the same time and share one relay — the
window onto the session's questions. The relay used to be closed when
the *first* lane finished, so a sibling lane still running had nowhere
to ask: its approval never reached the window, and the call stopped
with "agent call stopped to ask for approval of <tool>, and nobody can
answer" (campaign R664).

What we pin:

* a lane that finishes leaves the relay open for the lanes still
  running — the session still has somebody to relay to;
* the question a sibling lane asks *after* that still reaches the
  window, and the person's answer resumes the call;
* the relay is given back once the last lane is done.
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


class _Run:
    """What Agno hands back: a status, and the requirements it waits on."""

    def __init__(self, status: str, requirements: list[RunRequirement] | None = None) -> None:
        self.status = status
        self.content = ""
        self.metrics = None
        self.active_requirements = list(requirements or [])
        self.requirements = list(requirements or [])
        self.tools = []


def _requirement(tool_name: str, tool_args: dict) -> RunRequirement:
    """One Agno requirement: the tool call the run paused on."""
    return RunRequirement(
        tool_execution=ToolExecution(
            tool_name=tool_name,
            tool_args=tool_args,
            requires_confirmation=True,
        )
    )


def _request(payload: dict) -> AgentRequestEvent:
    return AgentRequestEvent(
        ts=1, run_id="wf_1", seq=0, payload=AgentRequestPayload.model_validate(payload)
    )


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


async def _wait_for_pause(transport: _Transport, count: int = 1) -> list[msg.RunPaused]:
    """The pauses the window was shown, once they are shown."""

    async def wait() -> list[msg.RunPaused]:
        while True:
            pauses = [m for m in transport.sent if isinstance(m, msg.RunPaused)]
            if len(pauses) >= count:
                return pauses
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(wait(), timeout=5)


async def test_a_lane_that_finishes_leaves_its_siblings_question_answered() -> None:
    """The sibling's question still reaches the window after a lane is done.

    One workflow, two lanes: the slow one finishes first, the other is
    still running and pauses on a tool call afterwards. The pause must
    reach the window, and the answer must resume the call.
    """
    release = asyncio.Event()
    first = _requirement("shell", {"cmd": "ls"})
    second = _requirement("shell", {"cmd": "cat"})

    team = MagicMock()

    async def arun(prompt: str, *args, **kwargs):
        if prompt == "slow":
            await release.wait()
            return _Run("completed")
        return _Run("paused", [first])

    team.arun = arun
    team.acontinue_run = AsyncMock(side_effect=[_Run("paused", [second]), _Run("completed")])

    session = _session(team)
    push, transport = _push()
    relay = WorkflowApprovalRelay(session, push)
    # One workflow, one relay, two lanes: as ``WorkflowRun`` builds it.
    slow = WorkflowAgentBridge(session, RunGate(), relay)
    sibling = WorkflowAgentBridge(session, RunGate(), relay)

    slow_call = asyncio.create_task(slow.answer(_request({"id": "r1", "prompt": "slow"})))
    # Let the slow lane open the relay before the sibling starts.
    await asyncio.sleep(0)
    sibling_call = asyncio.create_task(sibling.answer(_request({"id": "r2", "prompt": "sibling"})))

    # The sibling's first question reaches the window.
    first_pause = (await _wait_for_pause(transport))[0]

    # The slow lane finishes while the sibling is still running.
    release.set()
    await asyncio.wait_for(slow_call, timeout=5)
    assert session.sub_agent_hitl.has_relay is True, "the sibling still needs the relay"

    # The sibling asks again, now that its sibling lane is done.
    session.sub_agent_hitl.resolve(first_pause.requirements[0].requirement_id, "confirm")
    second_pause = (await _wait_for_pause(transport, count=2))[1]
    assert second_pause.requirements[0].tool_name == "shell"
    assert second_pause.requirements[0].tool_args == {"cmd": "cat"}

    session.sub_agent_hitl.resolve(second_pause.requirements[0].requirement_id, "confirm")
    reply = await asyncio.wait_for(sibling_call, timeout=5)

    assert reply.ok is True
    assert reply.error is None


async def test_the_relay_is_given_back_when_the_last_lane_is_done() -> None:
    """The session's relay is a count, and the workflow's share is one."""
    session = _session(MagicMock())
    push, _ = _push()
    relay = WorkflowApprovalRelay(session, push)

    await relay.open()
    await relay.open()
    assert session.sub_agent_hitl.has_relay is True

    await relay.close()
    # One lane is still running: the relay stays.
    assert session.sub_agent_hitl.has_relay is True

    await relay.close()
    assert session.sub_agent_hitl.has_relay is False


async def test_a_relay_that_was_never_opened_gives_back_nothing() -> None:
    """Closing a relay that never opened does not take the chat's share."""
    session = _session(MagicMock())
    push, _ = _push()
    relay = WorkflowApprovalRelay(session, push)
    # The chat's own multiplexer is relaying.
    session.sub_agent_hitl.open_relay()

    await relay.close()

    assert session.sub_agent_hitl.has_relay is True


@pytest.mark.parametrize("action", ["confirm", "reject"])
async def test_the_person_s_still_answers_the_lane_that_is_left(action: str) -> None:
    """Whoever is left relaying is asked, and their answer is the one."""
    release = asyncio.Event()
    requirement = _requirement("shell", {"cmd": "ls"})

    team = MagicMock()

    async def arun(prompt: str, *args, **kwargs):
        if prompt == "slow":
            await release.wait()
            return _Run("completed")
        return _Run("paused", [requirement])

    team.arun = arun
    team.acontinue_run = AsyncMock(return_value=_Run("completed"))

    session = _session(team)
    push, transport = _push()
    relay = WorkflowApprovalRelay(session, push)
    slow = WorkflowAgentBridge(session, RunGate(), relay)
    sibling = WorkflowAgentBridge(session, RunGate(), relay)

    slow_call = asyncio.create_task(slow.answer(_request({"id": "r3", "prompt": "slow"})))
    await asyncio.sleep(0)
    sibling_call = asyncio.create_task(sibling.answer(_request({"id": "r4", "prompt": "sibling"})))

    pause = (await _wait_for_pause(transport))[0]
    release.set()
    await asyncio.wait_for(slow_call, timeout=5)

    session.sub_agent_hitl.resolve(pause.requirements[0].requirement_id, action)
    reply = await asyncio.wait_for(sibling_call, timeout=5)

    assert reply.ok is (action == "confirm")
