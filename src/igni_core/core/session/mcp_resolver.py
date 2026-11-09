"""MCP tool resolver used by ``mcp_tool``-type hooks.

:class:`HookExecutor` calls it to look up an MCP server's tool by
(server, tool) name pair.

Owned by :class:`SessionHooks` and passed as
``HookExecutor(mcp_resolver=self.resolver.resolve)`` so
the executor never reaches into :class:`MCPClientManager`
internals directly. Which client holds which tool is the manager's
business — :meth:`MCPClientManager.get_tool` answers that — and this
class only carries the session's manager to the executor under the
name the hook itself speaks.

Rule 6 (oop_offender #6): a coordinator class replaces the free
function on the Session god-class.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from igni_core.core.mcp.client import MCPClientManager
from igni_core.core.mcp.naming import registered_tool_name


class MCPToolResolver:
    """Resolve MCP server tools for ``mcp_tool``-type hooks.

    Constructor takes a callable so the resolver reads
    :attr:`Session.mcp_manager` lazily — the manager is populated
    AFTER the hook executor is constructed in ``Session.__init__``,
    so an eager reference would go stale.
    """

    def __init__(self, mcp_manager_ref: Callable[[], MCPClientManager | None]) -> None:
        self._mcp_manager_ref = mcp_manager_ref

    def resolve(self, server: str, tool: str) -> Callable[..., Any] | None:
        """Return the callable for ``server::tool``, or ``None``.

        ``tool`` is the name the server itself gives the tool. The
        tool is registered under a namespaced name, so the name is
        translated before the manager is asked.

        ``None`` means "no such tool to call" — the server is not
        connected, the tool is not offered, or it was disabled. The
        hook handler treats that as a non-blocking skip, so a
        misconfigured hook degrades instead of failing the run.
        """
        mgr = self._mcp_manager_ref()
        if mgr is None:
            return None
        return mgr.get_tool(server, registered_tool_name(server, tool))


__all__ = ["MCPToolResolver"]
