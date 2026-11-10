"""The MCP servers — the client's view of them and their lifecycle.

The manager holds the configured servers and the connections to them;
the failure cache remembers why a connect failed; the lifecycle
coordinator is the connect / disconnect pair the rest of the session
calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from igni_core.core.mcp.client import MCPClientManager
from igni_core.core.mcp.config import MCPConfigLoader
from igni_core.core.network_policy import NetworkPolicy
from igni_core.core.session.mcp_ops import (
    McpFailureCache,
    McpLifecycleCoordinator,
    McpLifecycleDeps,
)

if TYPE_CHECKING:
    from igni_core.core.session.core import Session


class McpBuilder:
    """The MCP servers — the manager, the failure cache and the lifecycle."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def build_mcp(self) -> None:
        """The MCP client manager, with plugin-bundled configs merged in."""
        session = self._session
        session.mcp_manager = MCPClientManager(
            session.project_dir,
            group_mcps_dir=session.group_policy.dir_for("mcps"),
            network_policy=NetworkPolicy.from_settings(session.settings),
        )
        session.mcp_failures = McpFailureCache()
        session.plugin_loader.apply_to_mcp(
            MCPConfigLoader(session.project_dir),
            session.mcp_manager.configs,
            disabled=session.disabled_plugins,
        )

    def build_lifecycle(self) -> None:
        """Connect and disconnect the MCP servers, and rebuild the team after.

        A server that came up may have added tools, so the team is
        rebuilt once the set of servers has changed.
        """
        session = self._session
        session.mcp_lifecycle = McpLifecycleCoordinator(
            McpLifecycleDeps(
                mcp_manager=session.mcp_manager,
                rebuild=lambda: session.startup.rebuild_mcp(),
            )
        )


__all__ = ["McpBuilder"]
