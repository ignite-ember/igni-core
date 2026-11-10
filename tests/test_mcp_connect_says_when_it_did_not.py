"""``mcp_connect`` fails when the server did not connect.

It answered "Connected MCP: <name>" whatever happened — an unknown
server, one policy refused, one with no tools — and the extension host
announced ``mcp.changed: connected`` for it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.backend.server_mcp import McpController
from igni_core.core.mcp.schemas import MCPConnectResult


def _controller(result: MCPConnectResult) -> tuple[McpController, SimpleNamespace]:
    session = SimpleNamespace(
        mcp_manager=SimpleNamespace(connect=AsyncMock(return_value=result)),
        mcp_failures=SimpleNamespace(record=MagicMock()),
        rebuild_mcp=MagicMock(),
    )
    return McpController(session), session  # type: ignore[arg-type]


async def test_a_server_that_did_not_connect_fails_the_call_with_its_reason() -> None:
    controller, session = _controller(MCPConnectResult.failure("refused by policy"))

    with pytest.raises(ValueError, match="Could not connect MCP github: refused by policy"):
        await controller.connect("github")

    session.mcp_failures.record.assert_called_once()
    session.rebuild_mcp.assert_not_called()


async def test_a_server_that_connected_says_so() -> None:
    controller, session = _controller(MCPConnectResult.success(MagicMock()))

    info = await controller.connect("github")

    assert info.text == "Connected MCP: github"
    session.rebuild_mcp.assert_called_once_with()
