"""The arguments members hand igni's RPC handlers, one model per shape.

Field names are the wire keys the handlers read. Every one is dumped
whole — a key that is ``None`` is still sent, as the handler expects.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class RpcArgs(BaseModel):
    model_config = ConfigDict(frozen=True)


# ── chat ──────────────────────────────────────────────────────────


class SessionArgs(RpcArgs):
    session_id: str


class SearchChatArgs(RpcArgs):
    session_id: str
    query: str
    limit: int


class RunIdArgs(RpcArgs):
    run_id: str


class TruncateArgs(RpcArgs):
    session_id: str
    run_id: str


class CompactArgs(RpcArgs):
    ctx_tokens: int
    max_ctx: int


class LearningsArgs(RpcArgs):
    user_msg: str
    assistant_msg: str


class DiscardInterruptedArgs(RpcArgs):
    session_id: str
    message_id: str


class UploadArgs(RpcArgs):
    filename: str
    content_base64: str


class DispatchViewArgs(RpcArgs):
    action: str
    params: dict[str, Any]


# ── agents, workflows, scheduler, processes ───────────────────────


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


# ── workspace ─────────────────────────────────────────────────────


class PathArgs(RpcArgs):
    path: str


class CompleteArgs(RpcArgs):
    query: str
    limit: int


class QueryArgs(RpcArgs):
    query: str


# ── sessions, models, mcp, plugins ────────────────────────────────


class SwitchSessionArgs(RpcArgs):
    session_id: str


class SwitchModelArgs(RpcArgs):
    model_name: str


class ServerNameArgs(RpcArgs):
    server_name: str


class ToolEnabledArgs(RpcArgs):
    server: str
    tool: str
    enabled: bool


class PluginNameArgs(RpcArgs):
    name: str


class RefreshMarketplacesArgs(RpcArgs):
    #: ``None``: every marketplace.
    name: str | None


class PreviewPluginArgs(RpcArgs):
    source: str
    branch: str | None
    subdir: str | None


class InstallPluginArgs(RpcArgs):
    ref: str
    install_ref: str | None


class UpdatePluginArgs(RpcArgs):
    name: str
    install_ref: str | None


class PluginEnabledArgs(RpcArgs):
    name: str
    enabled: bool


class UrlArgs(RpcArgs):
    url: str


# ── permissions, notifications ────────────────────────────────────


class CheckPermissionArgs(RpcArgs):
    tool_name: str
    func_name: str
    tool_args: dict[str, Any]


class SaveRuleArgs(RpcArgs):
    rule: str
    level: str


class MarkReadArgs(RpcArgs):
    #: ``None``: every notification of ``source``, or all of them.
    ids: list[str] | None
    source: str | None


__all__ = [
    "AgentNameArgs",
    "CancelWorkflowArgs",
    "CheckPermissionArgs",
    "CompactArgs",
    "CompleteArgs",
    "DiscardInterruptedArgs",
    "DispatchViewArgs",
    "ExecuteTaskArgs",
    "InstallPluginArgs",
    "LearningsArgs",
    "MarkReadArgs",
    "PathArgs",
    "PidArgs",
    "PluginEnabledArgs",
    "PluginNameArgs",
    "PreviewPluginArgs",
    "PromoteArgs",
    "QueryArgs",
    "RefreshMarketplacesArgs",
    "RpcArgs",
    "RunIdArgs",
    "RunWorkflowArgs",
    "SaveRuleArgs",
    "ScheduledTaskArgs",
    "ScheduledTasksArgs",
    "SearchChatArgs",
    "ServerNameArgs",
    "SessionArgs",
    "ShellArgs",
    "SwitchModelArgs",
    "SwitchSessionArgs",
    "TailArgs",
    "ToolEnabledArgs",
    "TruncateArgs",
    "UpdatePluginArgs",
    "UploadArgs",
    "UrlArgs",
]
