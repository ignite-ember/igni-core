"""The concrete MCP client manager is what the extension protocols describe.

The protocols are structural: ``isinstance`` checks the methods are
there. So the manager must keep every method an install or an
extension's service calls, and a stand-in without one is not accepted.
"""

from __future__ import annotations

import pytest

from igni_core.core.mcp.client import MCPClientManager
from igni_core.extensions.mcp_manager import McpManager, McpServiceManager

INSTALL_METHODS = (
    "register_server",
    "get_client",
    "get_tools",
    "set_tool_enabled",
    "require_confirmation",
    "tool_annotations",
    "connect",
    "disconnect_one",
)


def _standin(*names: str) -> object:
    return type("Standin", (), {name: (lambda self, *a, **k: None) for name in names})()


def test_the_real_manager_is_an_install_manager_and_a_service_manager():
    manager = MCPClientManager()
    assert isinstance(manager, McpManager)
    assert isinstance(manager, McpServiceManager)


@pytest.mark.parametrize("missing", INSTALL_METHODS)
def test_a_manager_lacking_an_install_method_is_not_an_install_manager(missing):
    names = [name for name in INSTALL_METHODS if name != missing]
    assert not isinstance(_standin(*names), McpManager)


def test_reaching_a_service_needs_only_get_client():
    assert isinstance(_standin("get_client"), McpServiceManager)
    assert not isinstance(_standin("get_tools"), McpServiceManager)
    assert not isinstance(_standin(), McpServiceManager)


def test_an_install_manager_is_always_a_service_manager():
    assert isinstance(_standin(*INSTALL_METHODS), McpServiceManager)
