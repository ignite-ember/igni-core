"""MCP integration — Model Context Protocol client."""

from igni_core.core.mcp.approval import MCPApprovalManager
from igni_core.core.mcp.client import MCPClientManager
from igni_core.core.mcp.config import MCPConfigLoader, MCPPolicy, MCPServerConfig
from igni_core.core.mcp.schemas import (
    MCPConnectResult,
    MCPPrompt,
    MCPResource,
    MCPToolInfo,
)
from igni_core.core.mcp.stdio_binding import StdioMCPBinding
from igni_core.core.mcp.tool_filter import MCPToolFilter

__all__ = [
    "MCPApprovalManager",
    "MCPClientManager",
    "MCPConfigLoader",
    "MCPConnectResult",
    "MCPPolicy",
    "MCPPrompt",
    "MCPResource",
    "MCPServerConfig",
    "MCPToolFilter",
    "MCPToolInfo",
    "StdioMCPBinding",
]
