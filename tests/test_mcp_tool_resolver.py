"""Tests for the session's MCP tool resolver.

The resolver is the adapter between the session's MCP client manager and
the ``mcp_tool`` hook handler: it hands the manager the name a hook
carries and passes the callable the handler invokes back out.

Two things it is responsible for, and both are pinned here: the manager
is read when a tool is asked for (the session builds its hook executor
before it builds the manager), and the name is translated (a hook names
the tool the way the server does, the manager knows it by the name it
is registered under).
"""

from unittest.mock import create_autospec

from igni_core.core.mcp.client import MCPClientManager
from igni_core.core.session.mcp_resolver import MCPToolResolver


def _send_message(**kwargs: str) -> str:
    """The callable a manager hands back for a tool."""
    return "sent"


def _manager():
    """A manager whose ``get_tool`` answers with :func:`_send_message`."""
    manager = create_autospec(MCPClientManager, instance=True)
    manager.get_tool.return_value = _send_message
    return manager


class TestMCPToolResolver:
    def test_it_asks_the_manager_for_the_registered_name(self):
        """A hook names the tool the way the server does; the manager knows
        it by the name it is registered under."""
        manager = _manager()

        MCPToolResolver(lambda: manager).resolve("slack", "send_message")

        manager.get_tool.assert_called_once_with("slack", "mcp_slack_send_message")

    def test_a_registered_name_is_not_prefixed_twice(self):
        """A caller may already hold the registered name — prefixing it again
        would ask for a tool that does not exist."""
        manager = _manager()

        MCPToolResolver(lambda: manager).resolve("slack", "mcp_slack_send_message")

        manager.get_tool.assert_called_once_with("slack", "mcp_slack_send_message")

    def test_it_returns_the_callable_the_manager_names(self):
        """Identity matters: the handler awaits the SAME callable the manager
        holds, with no proxy or wrap in between."""
        manager = _manager()

        resolved = MCPToolResolver(lambda: manager).resolve("slack", "send_message")

        assert resolved is _send_message

    def test_it_returns_nothing_when_there_is_no_manager(self):
        """MCP compiled out, or the session built before MCP init completes."""
        manager = _manager()

        assert MCPToolResolver(lambda: None).resolve("slack", "send_message") is None
        manager.get_tool.assert_not_called()

    def test_it_returns_nothing_when_the_manager_has_no_such_tool(self):
        """Server not connected, tool not offered, or tool disabled — the
        handler turns a missing tool into a non-blocking skip."""
        manager = _manager()
        manager.get_tool.return_value = None

        assert MCPToolResolver(lambda: manager).resolve("slack", "send_message") is None

    def test_the_manager_is_read_when_the_tool_is_asked_for(self):
        """The session builds its hook executor before it builds the MCP
        manager, so a resolver that captured the manager eagerly would never
        see the one that exists."""
        box: dict[str, MCPClientManager | None] = {"manager": None}
        resolver = MCPToolResolver(lambda: box["manager"])

        assert resolver.resolve("slack", "send_message") is None

        manager = _manager()
        box["manager"] = manager

        assert resolver.resolve("slack", "send_message") is _send_message
        manager.get_tool.assert_called_once_with("slack", "mcp_slack_send_message")
