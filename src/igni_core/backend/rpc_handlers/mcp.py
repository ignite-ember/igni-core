"""MCP-server RPC handlers."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from igni_core.backend.rpc_handlers.base import RpcHandler, WireArgs, rpc
from igni_core.backend.schemas_mcp import (
    MCPServerSnapshot,
    MCPServerSummary,
    MCPToolToggleResult,
)
from igni_core.core.session.schemas import McpInitResult, McpServerStatus
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod


class McpServerArgs(BaseModel):
    """``mcp_connect`` / ``mcp_disconnect``: the server to act on."""

    model_config = ConfigDict(extra="ignore")

    server_name: str


class McpToolEnabledArgs(BaseModel):
    """``set_mcp_tool_enabled``: one tool of one server, on or off."""

    model_config = ConfigDict(extra="ignore")

    server: str
    tool: str
    enabled: bool


class McpRpcHandler(RpcHandler):
    """Ensure/connect/disconnect/status/details for MCP servers +
    per-tool enable toggling."""

    @rpc(RpcMethod.ENSURE_MCP)
    async def ensure_mcp(self, args: WireArgs) -> McpInitResult:
        return await self._controllers.mcp.ensure()

    @rpc(RpcMethod.MCP_CONNECT, args=McpServerArgs)
    async def mcp_connect(self, args: McpServerArgs) -> msg.Info:
        return await self._ctx.backend.mcp_connect(args.server_name)

    @rpc(RpcMethod.MCP_DISCONNECT, args=McpServerArgs)
    async def mcp_disconnect(self, args: McpServerArgs) -> msg.Info:
        return await self._controllers.mcp.disconnect(args.server_name)

    @rpc(RpcMethod.GET_MCP_STATUS)
    def mcp_status(self, args: WireArgs) -> list[McpServerStatus]:
        return self._ctx.backend.get_mcp_status()

    @rpc(RpcMethod.GET_MCP_SERVER_DETAILS)
    async def mcp_server_details(self, args: WireArgs) -> list[MCPServerSnapshot]:
        return await self._ctx.backend.get_mcp_server_details()

    @rpc(RpcMethod.GET_MCP_SERVERS)
    def mcp_servers(self, args: WireArgs) -> list[MCPServerSummary]:
        return self._ctx.backend.get_mcp_servers()

    @rpc(RpcMethod.SET_MCP_TOOL_ENABLED, args=McpToolEnabledArgs)
    def mcp_set_tool_enabled(self, args: McpToolEnabledArgs) -> MCPToolToggleResult:
        return self._controllers.mcp.set_tool_enabled(
            server=args.server,
            tool=args.tool,
            enabled=args.enabled,
        )
