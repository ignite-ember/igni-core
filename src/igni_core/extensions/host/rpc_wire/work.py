"""The handlers of agents, workflows, the scheduler and background
processes: their arguments, and the scheduler's task list."""

from __future__ import annotations

from typing import Any

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
    description: str


class TailArgs(RpcArgs):
    pid: int
    tail: int


class PidArgs(RpcArgs):
    pid: int


class ShellArgs(RpcArgs):
    command: str


__all__ = [
    "AgentNameArgs",
    "CancelWorkflowArgs",
    "ExecuteTaskArgs",
    "PidArgs",
    "PromoteArgs",
    "RunWorkflowArgs",
    "ScheduledTaskArgs",
    "ScheduledTasksArgs",
    "ShellArgs",
    "TailArgs",
]
