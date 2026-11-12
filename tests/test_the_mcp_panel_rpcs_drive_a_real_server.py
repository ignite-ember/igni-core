"""``McpController`` over a real stdio MCP server.

The panel's connect, disconnect, toggle, listing and per-tool switch run
against a real :class:`MCPClientManager` (the server script is the one
``test_an_mcp_server_is_connected_filtered_and_asked_for_more`` uses) and
a real :class:`McpFailureCache`. The session's team rebuild is counted,
because that is what makes a change live for the next agent turn.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from igni_core.backend.schemas_mcp import MCPToolToggleResult
from igni_core.backend.server_mcp import McpController
from igni_core.core.mcp.client import MCPClientManager
from igni_core.core.mcp.config import MCPPolicy
from igni_core.core.session.mcp_ops import (
    McpFailureCache,
    McpLifecycleCoordinator,
    McpLifecycleDeps,
)
from igni_core.core.session.schemas import McpInitResult
from tests.test_an_mcp_server_is_connected_filtered_and_asked_for_more import (
    DELETE,
    READ,
    _project,
)


@pytest.fixture
async def panel(tmp_path):
    manager = MCPClientManager(project_dir=_project(tmp_path), policy=MCPPolicy())
    rebuilds: list[int] = []

    async def ensure_mcp() -> McpInitResult:
        return McpInitResult(connected=["notes"], rebuilt=True)

    session = SimpleNamespace(
        mcp_manager=manager,
        mcp_failures=McpFailureCache(),
        startup=SimpleNamespace(
            rebuild_mcp=lambda: rebuilds.append(1),
            ensure_mcp=ensure_mcp,
        ),
        mcp_lifecycle=McpLifecycleCoordinator(
            McpLifecycleDeps(mcp_manager=manager, rebuild=lambda: None)
        ),
    )
    yield McpController(session), session, rebuilds  # type: ignore[arg-type]
    await manager.disconnect_all()


async def test_connect_then_disconnect_moves_the_listings_and_rebuilds_the_team(panel) -> None:
    controller, session, rebuilds = panel

    assert [(s.name, s.connected) for s in controller.servers()] == [("notes", False)]
    assert [(s.name, s.connected) for s in controller.status()] == [("notes", False)]

    assert (await controller.connect("notes")).text == "Connected MCP: notes"

    assert rebuilds == [1]
    assert [(s.name, s.connected) for s in controller.servers()] == [("notes", True)]
    assert [(s.name, s.connected) for s in controller.status()] == [("notes", True)]
    [detail] = await controller.server_details()
    assert (detail.name, detail.connected, detail.transport) == ("notes", True, "stdio")
    assert set(detail.tool_names) == {READ, DELETE}
    assert detail.error == ""
    assert [r.uri for r in detail.resources] == ["notes://index"]
    assert [p.name for p in detail.prompts] == ["summarise"]

    assert (await controller.disconnect("notes")).text == "Disconnected MCP: notes"

    assert rebuilds == [1, 1]
    assert [(s.name, s.connected) for s in controller.servers()] == [("notes", False)]
    [detail] = await controller.server_details()
    assert (detail.connected, detail.tool_names, detail.resources) == (False, [], [])


async def test_toggle_connects_and_disconnects_one_server_and_rebuilds(panel) -> None:
    controller, session, rebuilds = panel

    assert (await controller.toggle("notes", True)).text == "MCP connected: notes"
    assert session.mcp_manager.list_connected() == ["notes"]
    assert (await controller.toggle("notes", False)).text == "MCP disconnected: notes"
    assert session.mcp_manager.list_connected() == []
    assert rebuilds == [1, 1]


async def test_a_refused_connect_is_kept_for_the_details_and_cleared_by_a_disconnect(
    panel,
) -> None:
    controller, session, rebuilds = panel

    with pytest.raises(ValueError, match="Could not connect MCP ghost: "):
        await controller.connect("ghost")

    assert rebuilds == [], "nothing connected, so nothing to rebuild"
    assert session.mcp_failures.reason("ghost") != ""

    await controller.disconnect("ghost")

    assert session.mcp_failures.reason("ghost") == ""


async def test_a_tool_switched_off_through_the_panel_is_listed_as_disabled(panel) -> None:
    controller, session, _ = panel
    await controller.connect("notes")

    result = controller.set_tool_enabled("notes", DELETE, False)

    assert result == MCPToolToggleResult(server="notes", tool=DELETE, enabled=False)
    [detail] = await controller.server_details()
    assert detail.tools_disabled == [DELETE]
    assert set(detail.tool_names) == {READ, DELETE}
    assert set(session.mcp_manager.get_client("notes").functions) == {READ}

    controller.set_tool_enabled("notes", DELETE, True)

    [detail] = await controller.server_details()
    assert detail.tools_disabled == []


async def test_ensure_answers_with_the_startup_envelope(panel) -> None:
    controller, _, _ = panel

    assert await controller.ensure() == McpInitResult(connected=["notes"], rebuilt=True)


async def test_with_no_servers_configured_every_listing_is_empty(tmp_path) -> None:
    manager = MCPClientManager(project_dir=tmp_path, policy=MCPPolicy())
    session = SimpleNamespace(
        mcp_manager=manager,
        mcp_failures=McpFailureCache(),
        mcp_lifecycle=McpLifecycleCoordinator(
            McpLifecycleDeps(mcp_manager=manager, rebuild=lambda: None)
        ),
    )
    controller = McpController(session)  # type: ignore[arg-type]

    assert controller.servers() == []
    assert controller.status() == []
    assert await controller.server_details() == []
