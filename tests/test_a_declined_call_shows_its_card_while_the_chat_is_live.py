"""A call the person declines shows as a "Not run" card while the chat is live.

The window draws a tool card from ``tool_started`` and marks it rejected from
``tool_completed.is_rejected``. Agno's continuation streams neither for a
call rejected in the approval dialog, so the card appeared only after a
reload, from the history (campaign R641).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from igni_core.backend.hitl_controller import HitlController
from igni_core.backend.pending_requirements_store import PendingRequirementsStore
from igni_core.backend.schemas_pause import PendingRequirement
from igni_core.core.approvals.declined import declined_note
from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator
from igni_core.protocol import messages as msg


async def _nothing():
    if False:
        yield  # pragma: no cover


def _controller() -> HitlController:
    session = MagicMock()
    session.session_id = "s"
    session.main_team.acontinue_run = AsyncMock(return_value=_nothing())
    session.hook_executor.execute = AsyncMock(
        return_value=SimpleNamespace(should_continue=True, message="")
    )
    session.sub_agent_hitl = SubAgentHITLCoordinator()
    store = PendingRequirementsStore()
    req = MagicMock()
    req.tool_execution = SimpleNamespace(
        tool_name="run_shell_command", tool_args={"command": "echo x"}
    )
    store.register("req-1", PendingRequirement(req=req, run_id="run-1"))
    return HitlController(session, store)


async def _frames(action: str) -> list[msg.Message]:
    controller = _controller()
    decision = msg.HITLDecision(requirement_id="req-1", action=action, choice="once")
    return [m async for m in controller.resolve_batch([decision])]


async def test_a_rejected_call_gets_a_started_and_a_rejected_frame() -> None:
    frames = [
        m for m in await _frames("reject") if isinstance(m, (msg.ToolStarted, msg.ToolCompleted))
    ]

    started, completed = frames
    assert isinstance(started, msg.ToolStarted)
    assert started.tool_name == "run_shell_command"
    assert started.run_id == "run-1"
    assert isinstance(completed, msg.ToolCompleted)
    assert completed.is_rejected is True
    assert completed.full_result == declined_note("run_shell_command")
    assert completed.run_id == "run-1"


async def test_an_approved_call_gets_no_invented_frames() -> None:
    frames = await _frames("confirm")
    assert not [m for m in frames if isinstance(m, (msg.ToolStarted, msg.ToolCompleted))]
