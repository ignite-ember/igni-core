"""``igni_core.core.mcp`` exports exactly the names the rest of igni imports from it."""

from __future__ import annotations

import importlib

import igni_core.core.mcp as mcp

PUBLIC = {
    "MCPApprovalManager": "igni_core.core.mcp.approval",
    "MCPClientManager": "igni_core.core.mcp.client",
    "MCPConfigLoader": "igni_core.core.mcp.config",
    "MCPConnectResult": "igni_core.core.mcp.schemas",
    "MCPPolicy": "igni_core.core.mcp.config",
    "MCPPrompt": "igni_core.core.mcp.schemas",
    "MCPResource": "igni_core.core.mcp.schemas",
    "MCPServerConfig": "igni_core.core.mcp.config",
    "MCPToolFilter": "igni_core.core.mcp.tool_filter",
    "MCPToolInfo": "igni_core.core.mcp.schemas",
    "StdioMCPBinding": "igni_core.core.mcp.stdio_binding",
}


def test_the_package_exports_exactly_these_names() -> None:
    assert sorted(mcp.__all__) == sorted(PUBLIC)


def test_each_name_is_the_class_its_module_defines() -> None:
    for name, module in PUBLIC.items():
        assert getattr(mcp, name) is getattr(importlib.import_module(module), name), name
