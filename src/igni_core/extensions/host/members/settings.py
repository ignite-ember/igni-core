"""Models, MCP, plugins, hooks, permissions, policy and the account."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Self

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.members.base import Call, Members, call, get, put
from igni_core.extensions.host.members.rpc_args import (
    CheckPermissionArgs,
    InstallPluginArgs,
    PluginEnabledArgs,
    PluginNameArgs,
    PreviewPluginArgs,
    RefreshMarketplacesArgs,
    SaveRuleArgs,
    ServerNameArgs,
    SwitchModelArgs,
    ToolEnabledArgs,
    UpdatePluginArgs,
    UrlArgs,
)
from igni_core.extensions.host.spec import ErrorCode
from igni_core.protocol.rpc import RpcMethod


class ModelMembers(Members):
    @get("models.registry")
    async def registry(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_MODEL_REGISTRY, c=c)

    @get("models.current")
    async def current(self, c: Call) -> str:
        status = await self.rpc(RpcMethod.GET_STATUS, c=c)
        return str(status.get("model", ""))

    @put("models.current")
    async def set_current(self, c: Call) -> None:
        name = str(c.value or "")
        registry = await self.rpc(RpcMethod.GET_MODEL_REGISTRY, c=c)
        if name not in (registry.get("registry") or {}):
            raise HostModelError(ErrorCode.REFUSED, f"no model {name!r} is configured")
        await self.rpc_checked(RpcMethod.SWITCH_MODEL, SwitchModelArgs(model_name=name), c=c)


class McpMembers(Members):
    @get("mcp.servers")
    async def servers(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_MCP_SERVERS, c=c)

    @get("mcp.status")
    async def status(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_MCP_STATUS, c=c)

    @get("mcp.details")
    async def details(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_MCP_SERVER_DETAILS, c=c)

    @call("mcp.connect")
    async def connect(self, c: Call) -> None:
        await self.rpc(RpcMethod.MCP_CONNECT, ServerNameArgs(server_name=c.params["name"]), c=c)

    @call("mcp.disconnect")
    async def disconnect(self, c: Call) -> None:
        await self.rpc(RpcMethod.MCP_DISCONNECT, ServerNameArgs(server_name=c.params["name"]), c=c)

    @call("mcp.ensure")
    async def ensure(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.ENSURE_MCP, c=c)

    @call("mcp.set_tool_enabled")
    async def set_tool_enabled(self, c: Call) -> Any:
        args = ToolEnabledArgs(
            server=c.params["server"], tool=c.params["tool"], enabled=bool(c.params["on"])
        )
        return await self.rpc(RpcMethod.SET_MCP_TOOL_ENABLED, args, c=c)


class PluginMembers(Members):
    @get("plugins.installed")
    async def installed(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_PLUGIN_DETAILS, c=c)

    @get("plugins.marketplaces")
    async def marketplaces(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_MARKETPLACES, c=c)

    @call("plugins.contents")
    async def contents(self, c: Call) -> Any:
        args = PluginNameArgs(name=c.params["name"])
        return await self.rpc(RpcMethod.GET_PLUGIN_CONTENTS, args, c=c)

    @call("plugins.preview")
    async def preview(self, c: Call) -> Any:
        args = PreviewPluginArgs(
            source=c.params["source"],
            branch=c.params.get("branch"),
            subdir=c.params.get("subdir"),
        )
        return await self.rpc(RpcMethod.PREVIEW_PLUGIN, args, c=c)

    @call("plugins.install")
    async def install(self, c: Call) -> None:
        args = InstallPluginArgs(ref=c.params["ref"], install_ref=c.params.get("install_ref"))
        await self.rpc_checked(RpcMethod.INSTALL_PLUGIN, args, c=c)

    @call("plugins.update")
    async def update(self, c: Call) -> None:
        args = UpdatePluginArgs(name=c.params["name"], install_ref=c.params.get("install_ref"))
        await self.rpc_checked(RpcMethod.UPDATE_PLUGIN, args, c=c)

    @call("plugins.remove")
    async def remove(self, c: Call) -> None:
        await self.rpc_checked(RpcMethod.REMOVE_PLUGIN, PluginNameArgs(name=c.params["name"]), c=c)

    @call("plugins.set_enabled")
    async def set_enabled(self, c: Call) -> None:
        args = PluginEnabledArgs(name=c.params["name"], enabled=bool(c.params["on"]))
        await self.rpc_checked(RpcMethod.SET_PLUGIN_ENABLED, args, c=c)

    @call("plugins.add_marketplace")
    async def add_marketplace(self, c: Call) -> None:
        await self.rpc_checked(RpcMethod.ADD_MARKETPLACE, UrlArgs(url=c.params["url"]), c=c)

    @call("plugins.remove_marketplace")
    async def remove_marketplace(self, c: Call) -> None:
        args = PluginNameArgs(name=c.params["name"])
        await self.rpc_checked(RpcMethod.REMOVE_MARKETPLACE, args, c=c)

    @call("plugins.refresh_marketplaces")
    async def refresh_marketplaces(self, c: Call) -> None:
        args = RefreshMarketplacesArgs(name=c.params.get("name"))
        await self.rpc_checked(RpcMethod.REFRESH_MARKETPLACES, args, c=c)


class HookMembers(Members):
    @get("hooks.details")
    async def details(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_HOOKS_DETAILS, c=c)

    @call("hooks.reload")
    async def reload(self, c: Call) -> None:
        await self.rpc(RpcMethod.RELOAD_HOOKS, c=c)

    @call("hooks.fire_session_start")
    async def fire_session_start(self, c: Call) -> None:
        await self.rpc(RpcMethod.FIRE_SESSION_START_HOOK, c=c)


class PermissionLevel(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"

    @classmethod
    def of(cls, level: str) -> Self:
        if level not in {lv.value for lv in cls}:
            raise HostModelError(ErrorCode.INVALID, f"level is allow, ask or deny — not {level!r}")
        return cls(level)


class PermissionMembers(Members):
    @get("permissions.mode")
    async def mode(self, c: Call) -> str:
        status = await self.rpc(RpcMethod.GET_STATUS, c=c)
        return str(status.get("permission_mode", "default"))

    @call("permissions.check")
    async def check(self, c: Call) -> Any:
        args = CheckPermissionArgs(
            tool_name=c.params["tool_name"],
            func_name=c.params["func_name"],
            tool_args=c.params.get("tool_args") or {},
        )
        return await self.rpc(RpcMethod.CHECK_PERMISSION, args, c=c)

    @call("permissions.save_rule")
    async def save_rule(self, c: Call) -> None:
        level = PermissionLevel.of(str(c.params["level"]))
        args = SaveRuleArgs(rule=c.params["rule"], level=level.value)
        await self.rpc(RpcMethod.SAVE_PERMISSION_RULE, args, c=c)


class PolicyMembers(Members):
    @get("policy.pack")
    async def pack(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_GROUP_POLICY, c=c)


class AccountMembers(Members):
    @get("account.status")
    async def status(self, c: Call) -> Any:
        return await self.host.gateway.account(c.session_id)

    @call("account.sign_in")
    async def sign_in(self, c: Call) -> None:
        # Starts the person's own sign-in. What it produces — a URL, a
        # code, a session — is theirs, so nothing of it is returned.
        await self.rpc(RpcMethod.LOGIN, c=c)

    @call("account.sign_out")
    async def sign_out(self, c: Call) -> Any:
        await self.rpc(RpcMethod.CLEAR_CLOUD_CREDENTIALS, c=c)
        return await self.host.gateway.account(c.session_id)


__all__ = [
    "AccountMembers",
    "HookMembers",
    "McpMembers",
    "ModelMembers",
    "PermissionLevel",
    "PermissionMembers",
    "PluginMembers",
    "PolicyMembers",
]
