"""What :class:`ToolCallApprovalChannel` puts on screen and gives back.

The Bypass guard, the group-hook gate and the permission pipeline all ask
through this channel. These tests run the real channel over a real
:class:`SubAgentHITLCoordinator` and answer the dialog as the backend's
relay would: what is pending while the question is open, what the answer
turns into, and what is left behind afterwards.
"""

from __future__ import annotations

import asyncio

import pytest

from igni_core.core.session.tool_approval import (
    GuardRequirement,
    GuardToolExecution,
    ToolApprovalRequest,
    ToolCallApprovalChannel,
)
from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator
from igni_core.protocol import messages as msg


async def _open_question(coordinator, task):
    """Wait until the channel has put its question, and return what is pending."""
    await asyncio.wait_for(coordinator.new_arrival.wait(), timeout=2)
    (handle,) = coordinator.list_new_pending()
    return handle


async def _answered(action: msg.HITLAction, *, reason="why", follow_up=None, args=None):
    coordinator = SubAgentHITLCoordinator()
    coordinator.open_relay()
    task = asyncio.create_task(
        ToolCallApprovalChannel(coordinator).ask(
            "run_shell_command", args or {"command": "ls"}, reason, ["main"], follow_up=follow_up
        )
    )
    handle = await _open_question(coordinator, task)
    coordinator.resolve(handle.requirement_id, action)
    return await asyncio.wait_for(task, timeout=2), coordinator, handle


async def test_with_no_relay_nobody_can_answer_and_nothing_is_pushed():
    coordinator = SubAgentHITLCoordinator()

    answer = await asyncio.wait_for(
        ToolCallApprovalChannel(coordinator).ask("bash", {"command": "x"}, "r", []), timeout=1
    )

    assert answer is None
    assert coordinator.list_new_pending() == []
    assert coordinator.has_unresolved() is False


async def test_the_question_carries_the_call_the_reason_and_a_safety_check_path():
    coordinator = SubAgentHITLCoordinator()
    coordinator.open_relay()
    task = asyncio.create_task(
        ToolCallApprovalChannel(coordinator).ask(
            "run_shell_command", {"command": "rm -rf build"}, "deletes files", ["main", "worker"]
        )
    )
    handle = await _open_question(coordinator, task)

    requirement = handle.entry.requirement
    assert requirement.tool_execution.tool_name == "run_shell_command"
    assert requirement.tool_execution.tool_args == {"command": "rm -rf build"}
    assert requirement.details == "deletes files"
    assert requirement.details_pending is False
    assert handle.entry.run_id == "bypass-guard-run_shell_command"
    assert handle.entry.agent_path == ["main", "worker", "safety check"]

    coordinator.resolve(handle.requirement_id, msg.HITLAction.CONFIRM)
    assert await asyncio.wait_for(task, timeout=2) is True


async def test_an_empty_agent_path_is_just_the_safety_check():
    coordinator = SubAgentHITLCoordinator()
    coordinator.open_relay()
    task = asyncio.create_task(ToolCallApprovalChannel(coordinator).ask("t", {}, "r", []))
    handle = await _open_question(coordinator, task)

    assert handle.entry.agent_path == ["safety check"]
    assert handle.entry.requirement.tool_execution.tool_args == {}

    coordinator.resolve(handle.requirement_id, msg.HITLAction.REJECT)
    await asyncio.wait_for(task, timeout=2)


async def test_the_callers_agent_path_is_not_changed():
    coordinator = SubAgentHITLCoordinator()
    coordinator.open_relay()
    path = ["main"]
    task = asyncio.create_task(ToolCallApprovalChannel(coordinator).ask("t", {}, "r", path))
    handle = await _open_question(coordinator, task)
    coordinator.resolve(handle.requirement_id, msg.HITLAction.CONFIRM)
    await asyncio.wait_for(task, timeout=2)

    assert path == ["main"]


async def test_approving_answers_true_and_rejecting_answers_false():
    approved, _, _ = await _answered(msg.HITLAction.CONFIRM)
    denied, _, _ = await _answered(msg.HITLAction.REJECT)

    assert approved is True
    assert denied is False


async def test_the_question_is_gone_once_it_is_answered():
    _, coordinator, handle = await _answered(msg.HITLAction.CONFIRM)

    assert coordinator.pending.entry(handle.requirement_id) is None
    assert coordinator.has_unresolved() is False


async def test_the_question_is_cleaned_up_when_the_waiting_is_cancelled():
    coordinator = SubAgentHITLCoordinator()
    coordinator.open_relay()
    task = asyncio.create_task(ToolCallApprovalChannel(coordinator).ask("t", {}, "r", []))
    handle = await _open_question(coordinator, task)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert coordinator.pending.entry(handle.requirement_id) is None


async def test_the_question_is_cleaned_up_when_the_waiting_raises(monkeypatch):
    coordinator = SubAgentHITLCoordinator()
    coordinator.open_relay()

    async def broken(req_id: str):
        raise RuntimeError("relay died")

    monkeypatch.setattr(coordinator, "wait_resolved", broken)
    with pytest.raises(RuntimeError, match="relay died"):
        await ToolCallApprovalChannel(coordinator).ask("t", {}, "r", [])

    assert coordinator.pending.ids() == []


class TestTheModelsDescription:
    async def test_the_dialog_opens_pending_with_no_text_and_the_description_replaces_it(self):
        gate = asyncio.Event()

        async def follow_up() -> str | None:
            await gate.wait()
            return "Pushes over origin/main"

        coordinator = SubAgentHITLCoordinator()
        coordinator.open_relay()
        task = asyncio.create_task(
            ToolCallApprovalChannel(coordinator).ask(
                "t", {}, "stock reason", [], follow_up=follow_up
            )
        )
        handle = await _open_question(coordinator, task)
        requirement = handle.entry.requirement
        assert requirement.details_pending is True and requirement.details == ""

        gate.set()
        for _ in range(100):
            if requirement.details:
                break
            await asyncio.sleep(0.01)
        assert requirement.details == "Pushes over origin/main"

        coordinator.resolve(handle.requirement_id, msg.HITLAction.CONFIRM)
        assert await asyncio.wait_for(task, timeout=2) is True

    @pytest.mark.parametrize("nothing", [None, ""])
    async def test_no_description_leaves_the_plain_reason(self, nothing):
        async def follow_up() -> str | None:
            return nothing

        coordinator = SubAgentHITLCoordinator()
        coordinator.open_relay()
        task = asyncio.create_task(
            ToolCallApprovalChannel(coordinator).ask(
                "t", {}, "stock reason", [], follow_up=follow_up
            )
        )
        handle = await _open_question(coordinator, task)
        for _ in range(100):
            if handle.entry.requirement.details:
                break
            await asyncio.sleep(0.01)

        assert handle.entry.requirement.details == "stock reason"
        coordinator.resolve(handle.requirement_id, msg.HITLAction.CONFIRM)
        await asyncio.wait_for(task, timeout=2)

    async def test_a_description_that_fails_does_not_break_the_question(self):
        async def follow_up() -> str | None:
            raise RuntimeError("model unreachable")

        answer, coordinator, handle = await _answered(msg.HITLAction.CONFIRM, follow_up=follow_up)

        assert answer is True
        assert coordinator.pending.entry(handle.requirement_id) is None

    async def test_the_explanation_task_is_cancelled_when_the_person_answers_first(self):
        started, finished = asyncio.Event(), []

        async def follow_up() -> str | None:
            started.set()
            try:
                await asyncio.sleep(3600)
            finally:
                finished.append("cancelled")
            return "never"

        coordinator = SubAgentHITLCoordinator()
        coordinator.open_relay()
        task = asyncio.create_task(
            ToolCallApprovalChannel(coordinator).ask("t", {}, "r", [], follow_up=follow_up)
        )
        handle = await _open_question(coordinator, task)
        await started.wait()
        coordinator.resolve(handle.requirement_id, msg.HITLAction.REJECT)
        await asyncio.wait_for(task, timeout=2)
        await asyncio.sleep(0.05)

        assert finished == ["cancelled"]


class TestTheRequirementModels:
    def test_confirm_sets_confirmed_and_reject_clears_it_whatever_the_note(self):
        requirement = GuardRequirement(
            tool_execution=GuardToolExecution(tool_name="t"), details="d"
        )
        assert requirement.confirmed is False and requirement.details_pending is False

        requirement.confirm()
        assert requirement.confirmed is True
        requirement.reject(note="user said no")
        assert requirement.confirmed is False
        requirement.confirm()
        requirement.reject()
        assert requirement.confirmed is False

    def test_the_tool_execution_and_the_request_default_to_no_args_and_no_path(self):
        assert GuardToolExecution(tool_name="t").tool_args == {}
        request = ToolApprovalRequest(tool_name="t", reason="r")
        assert request.args == {} and request.agent_path == []
