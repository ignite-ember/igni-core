"""MCP-server RPC handlers."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from igni_core.backend.rpc_binding import NoArgs, rpc
from igni_core.backend.rpc_handlers.base import RpcHandler
from igni_core.backend.schemas_mcp import (
    McpApproval,
    MCPServerSnapshot,
    MCPServerSummary,
    MCPToolToggleResult,
)
from igni_core.core.session.schemas import McpInitResult, McpServerStatus
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcDuration, RpcMethod, RpcScope


class McpServerArgs(BaseModel):
    """Arguments naming a configured MCP server."""

    model_config = ConfigDict(extra="ignore")

    server_name: str = Field(description="The server's name in the MCP configuration.")


class McpToolEnabledArgs(BaseModel):
    """Arguments for switching one MCP server tool on or off."""

    model_config = ConfigDict(extra="ignore")

    server: str = Field(description="Name of the MCP server; not checked to exist.")
    tool: str = Field(description="Name of the server's tool; not checked to exist.")
    enabled: bool = Field(description="The tool's new state: `true` lets the agent use it.")


class McpApprovalArgs(BaseModel):
    """Arguments approving a configured MCP server, or withdrawing its approval."""

    model_config = ConfigDict(extra="ignore")

    server_name: str = Field(description="The server's name in the MCP configuration.")
    approved: bool = Field(
        description="`true` approves it; `false` withdraws its approval and disconnects it."
    )


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

    @rpc(RpcMethod.LIST_MCP_APPROVALS, args=NoArgs, scope=RpcScope.SESSION)
    def list_mcp_approvals(self, args: NoArgs) -> list[McpApproval]:
        """List this session's MCP servers that need a first-use approval.

        Takes no arguments. Returns one row per configured server — its
        ``name``, where it is configured (``source``, ``source_path``) and
        whether it is ``approved``. A server from a project's or a plugin's
        ``.mcp.json`` runs a command or opens a connection as the person, so
        it never connects until approved; without a terminal to ask on,
        igni's window asks instead and answers with ``set_mcp_approval``. An
        extension's own service is approved by its install and not listed.
        """
        return self._controllers.mcp.approvals()

    @rpc(
        RpcMethod.SET_MCP_APPROVAL,
        duration=RpcDuration.IO,
        args=McpApprovalArgs,
        scope=RpcScope.SESSION,
    )
    async def set_mcp_approval(self, args: McpApprovalArgs) -> McpApproval:
        """Approve a configured MCP server, or withdraw its approval.

        ``server_name`` names the server; ``approved`` is the answer.
        Approving records it for that server from that configuration file
        (in ``~/.igni/mcp-approved.json``, which every session reads again
        when it changes); the server connects on the next ``mcp_connect`` or
        start. Withdrawing disconnects it if it is connected. Returns the
        server's row as ``list_mcp_approvals`` would. Fails for a server not
        configured, an extension's, or the user's own ``~/.igni/.mcp.json``,
        which is always trusted. Only igni's window may call it.
        """
        return await self._controllers.mcp.set_approval(args.server_name, args.approved)
