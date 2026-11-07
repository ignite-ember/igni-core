"""The handlers of agents, workflows, the scheduler and background
processes: their arguments, and the scheduler's task list."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from igni_core.extensions.host.rpc_wire.base import RpcArgs


class PromoteArgs(RpcArgs):
    name: str
    target: str


class AgentNameArgs(RpcArgs):
    name: str


class RunWorkflowArgs(RpcArgs):
    name: str
    args: dict[str, Any]
    session_id: str


class CancelWorkflowArgs(RpcArgs):
    workflow_run_id: str


class ScheduledTasksArgs(RpcArgs):
    include_done: bool


class ScheduledTaskArgs(RpcArgs):
    task_id: str


class ExecuteTaskArgs(RpcArgs):
    #: As the scheduled task carries it.
    description: Any


class TailArgs(RpcArgs):
    pid: int
    tail: int


class PidArgs(RpcArgs):
    pid: int


class ShellArgs(RpcArgs):
    command: str


class ScheduledTask(BaseModel):
    """One scheduled task, as far as running it now needs."""

    key: str
    #: As the backend has it: what ``execute_scheduled_task`` is handed back.
    description: Any


class ScheduledTasks(BaseModel):
    """``get_scheduled_tasks``, by id."""

    tasks: list[ScheduledTask]

    @classmethod
    def from_rpc(cls, found: Any) -> ScheduledTasks:
        return cls(
            tasks=[
                ScheduledTask(
                    key=str(t.get("id") or t.get("task_id")),
                    description=t.get("description", ""),
                )
                for t in found or []
            ]
        )

    def find(self, task_id: str) -> ScheduledTask | None:
        return next((t for t in self.tasks if t.key == task_id), None)


__all__ = [
    "AgentNameArgs",
    "CancelWorkflowArgs",
    "ExecuteTaskArgs",
    "PidArgs",
    "PromoteArgs",
    "RunWorkflowArgs",
    "ScheduledTask",
    "ScheduledTaskArgs",
    "ScheduledTasks",
    "ScheduledTasksArgs",
    "ShellArgs",
    "TailArgs",
]
