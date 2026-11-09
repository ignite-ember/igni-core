"""MCP-server RPC handlers."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from igni_core.backend.rpc_binding import NoArgs, rpc
from igni_core.backend.rpc_handlers.base import RpcHandler
from igni_core.backend.schemas_mcp import (
    MCPServerSnapshot,
    MCPServerSummary,
    MCPToolToggleResult,
)
from igni_core.core.session.schemas import McpInitResult, McpServerStatus
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcDuration, RpcMethod, RpcScope


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

    @rpc(RpcMethod.ENSURE_MCP, duration=RpcDuration.LONG, args=NoArgs, scope=RpcScope.SESSION)
    async def ensure_mcp(self, args: NoArgs) -> McpInitResult:
        """Connect this session's configured MCP servers, once.

        Takes no arguments. The first call connects every configured server
        and rebuilds the agent with their tools; later calls do nothing.
        Returns the servers ``connected``, the ones that ``failed`` with
        their errors, whether the agent was ``rebuilt``, and, when nothing
        was done, ``skipped_reason`` (``already_initialized``,
        ``no_configured_servers``, ``no_clients_connected``).
        """
        return await self._controllers.mcp.ensure()

    @rpc(
        RpcMethod.MCP_CONNECT,
        duration=RpcDuration.NETWORK,
        args=McpServerArgs,
        scope=RpcScope.SESSION,
    )
    async def mcp_connect(self, args: McpServerArgs) -> msg.Info:
        """Connect one configured MCP server to this session.

        ``server_name`` is the server's name in the MCP configuration.
        Returns an ``info`` message, sent in place of an ``rpc_response``,
        once it is connected. Fails with the reason when it did not connect
        (not configured, refused by policy, no tools, approval denied); the
        reason is also ``get_mcp_server_details``'s ``error`` for it.
        """
        return await self._ctx.backend.mcp_connect(args.server_name)

    @rpc(
        RpcMethod.MCP_DISCONNECT,
        duration=RpcDuration.IO,
        args=McpServerArgs,
        scope=RpcScope.SESSION,
    )
    async def mcp_disconnect(self, args: McpServerArgs) -> msg.Info:
        """Disconnect one MCP server from this session.

        ``server_name`` is the server's name in the MCP configuration.
        Returns an ``info`` message, sent in place of an ``rpc_response``,
        whether or not the server was connected.
        """
        return await self._controllers.mcp.disconnect(args.server_name)

    @rpc(RpcMethod.GET_MCP_STATUS, args=NoArgs, scope=RpcScope.SESSION)
    def mcp_status(self, args: NoArgs) -> list[McpServerStatus]:
        """List this session's MCP servers and whether each is connected.

        Takes no arguments. Returns one ``{name, connected}`` row per
        server, in no particular order.
        """
        return self._ctx.backend.get_mcp_status()

    @rpc(
        RpcMethod.GET_MCP_SERVER_DETAILS,
        duration=RpcDuration.NETWORK,
        args=NoArgs,
        scope=RpcScope.SESSION,
    )
    async def mcp_server_details(self, args: NoArgs) -> list[MCPServerSnapshot]:
        """Describe each of this session's MCP servers in full.

        Takes no arguments. Returns one row per server: whether it is
        connected, its transport, its tools (names, descriptions, the ones
        switched off), its resources and prompts, the last error connecting
        it, and whether policy blocks it. Each server is asked in turn, so
        a slow one slows the answer.
        """
        return await self._ctx.backend.get_mcp_server_details()

    @rpc(RpcMethod.GET_MCP_SERVERS, args=NoArgs, scope=RpcScope.SESSION)
    def mcp_servers(self, args: NoArgs) -> list[MCPServerSummary]:
        """List this session's configured MCP servers, in configuration order.

        Takes no arguments. Returns one ``{name, connected}`` row per
        server.
        """
        return self._ctx.backend.get_mcp_servers()

    @rpc(
        RpcMethod.SET_MCP_TOOL_ENABLED,
        duration=RpcDuration.IO,
        args=McpToolEnabledArgs,
        scope=RpcScope.SESSION,
    )
    def mcp_set_tool_enabled(self, args: McpToolEnabledArgs) -> MCPToolToggleResult:
        """Switch one MCP server's tool on or off for this project.

        ``server`` and ``tool`` name the tool; ``enabled`` is its new state.
        The choice is saved in the project's ``.igni/mcp-tool-state.json``.
        Returns the ``server``, ``tool`` and ``enabled`` as recorded; the
        server and tool are not checked to exist. Fails when the state file
        cannot be written.
        """
        return self._controllers.mcp.set_tool_enabled(
            server=args.server,
            tool=args.tool,
            enabled=args.enabled,
        )
