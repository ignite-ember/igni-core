"""The plan, tasks and loop; commands, agents, workflows, the scheduler
and background processes."""

from __future__ import annotations

from typing import Any

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.members.answers import PlanSnapshot
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.choice import Choice
from igni_core.extensions.host.members.tag import call, get
from igni_core.extensions.host.rpc_wire.chat import RunIdArgs
from igni_core.extensions.host.rpc_wire.work import (
    AgentNameArgs,
    CancelWorkflowArgs,
    ExecuteTaskArgs,
    PidArgs,
    PromoteArgs,
    RunWorkflowArgs,
    ScheduledTaskArgs,
    ScheduledTasks,
    ScheduledTasksArgs,
    ShellArgs,
    TailArgs,
)
from igni_core.extensions.host.spec import ErrorCode
from igni_core.protocol.rpc import RpcMethod


class PlanMembers(Members):
    @get("plan.latest")
    async def latest(self, c: Call) -> PlanSnapshot:
        return PlanSnapshot.from_rpc(await self.rpc(RpcMethod.GET_LATEST_PLAN, c=c))

    @call("plan.approve")
    async def approve(self, c: Call) -> Any:
        args = RunIdArgs(run_id=c.params.get("run_id") or "")
        return await self.rpc(RpcMethod.APPROVE_PLAN, args, c=c)

    @call("plan.dismiss")
    async def dismiss(self, c: Call) -> Any:
        args = RunIdArgs(run_id=c.params.get("run_id") or "")
        return await self.rpc(RpcMethod.DISMISS_PLAN, args, c=c)


class TaskMembers(Members):
    @get("tasks.todos")
    async def todos(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_TODOS, c=c)


class LoopMembers(Members):
    """``loop.changed`` is emitted for every loop write, the person's and
    these, by :class:`~igni_core.extensions.host.broadcasts.rpc_writes.RpcWriteSignals`."""

    @get("loop.status")
    async def status(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.LOOP_STATUS, c=c)

    @call("loop.pause")
    async def pause(self, c: Call) -> bool:
        return bool(await self.rpc(RpcMethod.LOOP_PAUSE, c=c))

    @call("loop.resume")
    async def resume(self, c: Call) -> str:
        return str(await self.rpc(RpcMethod.LOOP_RESUME, c=c) or "")

    @call("loop.cancel_pending")
    async def cancel_pending(self, c: Call) -> bool:
        return bool(await self.rpc(RpcMethod.CANCEL_PENDING_LOOP, c=c))

    @call("loop.pop_pending")
    async def pop_pending(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.POP_PENDING_LOOP_ITERATION, c=c)


class CommandMembers(Members):
    @get("commands.list")
    async def command_list(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_SLASH_COMMANDS, c=c)

    @get("commands.skills")
    async def skills(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_SKILL_NAMES, c=c)

    @get("commands.skill_definitions")
    async def skill_definitions(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_SKILL_DEFINITIONS, c=c)

    @get("commands.skill_details")
    async def skill_details(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_SKILL_DETAILS, c=c)

    @call("commands.run")
    async def run(self, c: Call) -> Any:
        command = str(c.params["command"]).strip()
        if not command.startswith("/"):
            raise HostModelError(ErrorCode.INVALID, f"{command!r} is not a slash command")
        return await self.host.gateway.command(command, session_id=c.session_id)


class PromoteTarget(Choice):
    """Where ``promote_ephemeral`` may keep an agent, as ``/agents promote
    --project | --personal`` and the panel's RPC ``target`` name them."""

    PROJECT = "project"
    PERSONAL = "personal"

    @classmethod
    def refusal(cls, value: str) -> str:
        return (
            f"no promote target {value!r}: project (.igni/agents/, the default) "
            "or personal (.igni/agents.local/)"
        )


class AgentMembers(Members):
    @get("agents.details")
    async def details(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_AGENT_DETAILS, c=c)

    @call("agents.promote_ephemeral")
    async def promote(self, c: Call) -> Any:
        target = PromoteTarget.of(c.params.get("target") or PromoteTarget.PROJECT.value)
        args = PromoteArgs(name=c.params["name"], target=target.value)
        return await self.rpc(RpcMethod.PROMOTE_EPHEMERAL_AGENT, args, c=c)

    @call("agents.discard_ephemeral")
    async def discard(self, c: Call) -> Any:
        args = AgentNameArgs(name=c.params["name"])
        return await self.rpc(RpcMethod.DISCARD_EPHEMERAL_AGENT, args, c=c)


class WorkflowMembers(Members):
    @get("workflows.list")
    async def workflow_list(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.LIST_WORKFLOWS, c=c)

    @call("workflows.run")
    async def run(self, c: Call) -> Any:
        args = RunWorkflowArgs(
            name=c.params["name"], args=c.params.get("input") or {}, session_id=c.session_id
        )
        return await self.rpc(RpcMethod.RUN_WORKFLOW, args, c=c)

    @call("workflows.cancel")
    async def cancel(self, c: Call) -> bool:
        args = CancelWorkflowArgs(workflow_run_id=c.params["workflow_run_id"])
        found = await self.rpc(RpcMethod.CANCEL_WORKFLOW, args, c=c)
        return bool(found.get("cancelled")) if isinstance(found, dict) else bool(found)


class SchedulerMembers(Members):
    @get("scheduler.tasks")
    async def tasks(self, c: Call) -> Any:
        return await self._listing(c)

    @call("scheduler.start")
    async def start(self, c: Call) -> None:
        await self.rpc(RpcMethod.START_SCHEDULER, c=c)

    @call("scheduler.run_now")
    async def run_now(self, c: Call) -> str:
        task_id = str(c.params["task_id"])
        task = ScheduledTasks.from_rpc(await self._listing(c)).find(task_id)
        if task is None:
            raise HostModelError(ErrorCode.NOT_FOUND, f"no scheduled task {task_id!r}")
        args = ExecuteTaskArgs(description=task.description)
        return str(await self.rpc(RpcMethod.EXECUTE_SCHEDULED_TASK, args, c=c))

    @call("scheduler.cancel")
    async def cancel(self, c: Call) -> None:
        args = ScheduledTaskArgs(task_id=c.params["task_id"])
        await self.rpc(RpcMethod.CANCEL_SCHEDULED_TASK, args, c=c)

    async def _listing(self, c: Call) -> Any:
        """``get_scheduled_tasks``, done ones included, as the backend gives it."""
        return await self.rpc(
            RpcMethod.GET_SCHEDULED_TASKS, ScheduledTasksArgs(include_done=True), c=c
        )


class ProcessMembers(Members):
    @get("processes.list")
    async def process_list(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.LIST_BACKGROUND_PROCESSES, c=c)

    @call("processes.tail")
    async def tail(self, c: Call) -> Any:
        args = TailArgs(pid=c.params["pid"], tail=c.params.get("lines") or 200)
        return await self.rpc(RpcMethod.READ_PROCESS_TAIL, args, c=c)

    @call("processes.stop")
    async def stop(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.STOP_BACKGROUND_PROCESS, PidArgs(pid=c.params["pid"]), c=c)

    @call("processes.run")
    async def run(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.RUN_SHELL, ShellArgs(command=c.params["command"]), c=c)


__all__ = [
    "AgentMembers",
    "CommandMembers",
    "LoopMembers",
    "PlanMembers",
    "ProcessMembers",
    "PromoteTarget",
    "SchedulerMembers",
    "TaskMembers",
    "WorkflowMembers",
]
