"""``host.mcp``: the MCP servers, their tools, and which are connected."""

from __future__ import annotations

from igni_core.backend.schemas_mcp import MCPServerSnapshot, MCPServerSummary, MCPToolToggleResult
from igni_core.core.session.schemas import McpInitResult, McpServerStatus
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.reads import read_answer, read_answers
from igni_core.extensions.host.members.tag import call, get
from igni_core.extensions.host.rpc_wire.settings import ServerNameArgs, ToolEnabledArgs
from igni_core.protocol.rpc import RpcMethod


class McpMembers(Members):
    @get("mcp.servers")
    async def servers(self, c: Call) -> list[MCPServerSummary]:
        return read_answers(
            MCPServerSummary,
            await self.rpc(RpcMethod.GET_MCP_SERVERS, c=c),
            method="mcp.servers",
        )

    @get("mcp.status")
    async def status(self, c: Call) -> list[McpServerStatus]:
        return read_answers(
            McpServerStatus,
            await self.rpc(RpcMethod.GET_MCP_STATUS, c=c),
            method="mcp.status",
        )

    @get("mcp.details")
    async def details(self, c: Call) -> list[MCPServerSnapshot]:
        return read_answers(
            MCPServerSnapshot,
            await self.rpc(RpcMethod.GET_MCP_SERVER_DETAILS, c=c),
            method="mcp.details",
        )

    @call("mcp.connect")
    async def connect(self, c: Call) -> None:
        await self.rpc(RpcMethod.MCP_CONNECT, ServerNameArgs(server_name=c.params["name"]), c=c)

    @call("mcp.disconnect")
    async def disconnect(self, c: Call) -> None:
        await self.rpc(RpcMethod.MCP_DISCONNECT, ServerNameArgs(server_name=c.params["name"]), c=c)

    @call("mcp.ensure")
    async def ensure(self, c: Call) -> McpInitResult:
        return read_answer(
            McpInitResult,
            await self.rpc(RpcMethod.ENSURE_MCP, c=c),
            method="mcp.ensure",
        )

    @call("mcp.set_tool_enabled")
    async def set_tool_enabled(self, c: Call) -> MCPToolToggleResult:
        args = ToolEnabledArgs(
            server=c.params["server"], tool=c.params["tool"], enabled=bool(c.params["on"])
        )
        return read_answer(
            MCPToolToggleResult,
            await self.rpc(RpcMethod.SET_MCP_TOOL_ENABLED, args, c=c),
            method="mcp.set_tool_enabled",
        )


__all__ = ["McpMembers"]
