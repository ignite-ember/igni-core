"""The handlers of models, MCP, plugins and permissions: their arguments."""

from __future__ import annotations

from typing import Any

from igni_core.extensions.host.rpc_wire.base import RpcArgs


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


class CheckPermissionArgs(RpcArgs):
    tool_name: str
    func_name: str
    tool_args: dict[str, Any]


class SaveRuleArgs(RpcArgs):
    rule: str
    level: str


__all__ = [
    "CheckPermissionArgs",
    "InstallPluginArgs",
    "PluginEnabledArgs",
    "PluginNameArgs",
    "PreviewPluginArgs",
    "RefreshMarketplacesArgs",
    "SaveRuleArgs",
    "ServerNameArgs",
    "SwitchModelArgs",
    "ToolEnabledArgs",
    "UpdatePluginArgs",
    "UrlArgs",
]
