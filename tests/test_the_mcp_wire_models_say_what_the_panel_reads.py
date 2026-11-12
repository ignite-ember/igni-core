"""The MCP panel's rows say what the manager knows about a server.

The server is configured through a real ``.mcp.json`` and a real
:class:`MCPClientManager`; one is connected over stdio, the others are not.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from igni_core.backend.schemas_mcp import MCPServerSnapshot, MCPServerSummary, MCPToolToggleResult
from igni_core.core.mcp.approval import MCPApprovalManager
from igni_core.core.mcp.client import MCPClientManager
from igni_core.core.mcp.config import MCPPolicy
from igni_core.core.session.mcp_ops import McpFailureCache

SERVER = r"""
from mcp.server.mcpserver import MCPServer

server = MCPServer("notes")

@server.tool()
def read_note(title: str) -> str:
    "Read one note by its title."
    return title

@server.tool()
def delete_note(title: str) -> str:
    "Delete one note."
    return title

@server.resource("notes://index", mime_type="text/plain", description="Every note's title.")
def index() -> str:
    return "groceries"

@server.prompt(description="Summarise one note.")
def summarise(title: str) -> str:
    return title

server.run()
"""


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "server.py").write_text(SERVER)
    config = project / ".mcp.json"
    command = {"command": sys.executable, "args": [str(project / "server.py")]}
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "notes": command,
                    "idle": command,
                    "banned-one": command,
                    "web": {"type": "sse", "url": "http://localhost:9/sse"},
                }
            }
        )
    )
    MCPApprovalManager().approve("notes", str(config))
    return project


@pytest.fixture
async def manager(tmp_path):
    mcp = MCPClientManager(project_dir=_project(tmp_path), policy=MCPPolicy(denied=["banned-*"]))
    yield mcp
    await mcp.disconnect_all()


async def test_a_connected_server_lists_its_tools_resources_and_prompts(manager) -> None:
    assert (await manager.connect("notes")).ok
    manager.set_tool_enabled("notes", "mcp_notes_delete_note", False)

    snap = await MCPServerSnapshot.from_manager(manager, "notes")

    assert snap.name == "notes" and snap.connected is True
    assert snap.transport == "stdio"
    assert set(snap.tool_names) == {"mcp_notes_read_note", "mcp_notes_delete_note"}
    assert snap.tool_descriptions["mcp_notes_read_note"] == "Read one note by its title."
    assert snap.tools_disabled == ["mcp_notes_delete_note"]
    assert [r.uri for r in snap.resources] == ["notes://index"]
    assert [p.name for p in snap.prompts] == ["summarise"]
    assert (snap.error, snap.policy_blocked) == ("", False)


async def test_a_server_that_is_not_connected_has_no_tools_resources_or_prompts(manager) -> None:
    snap = await MCPServerSnapshot.from_manager(manager, "idle")

    assert snap.connected is False
    assert snap.tool_names == []
    assert snap.tool_descriptions == {}
    assert snap.resources == [] and snap.prompts == []
    assert snap.error == ""


async def test_the_transport_is_the_configured_one_and_unknown_without_a_config(manager) -> None:
    assert (await MCPServerSnapshot.from_manager(manager, "web")).transport == "sse"
    assert (await MCPServerSnapshot.from_manager(manager, "ghost")).transport == "unknown"


async def test_the_error_is_the_last_connect_failure_the_session_recorded(manager) -> None:
    refused = await manager.connect("banned-one")
    assert not refused.ok and refused.reason
    failures = McpFailureCache()
    failures.record("banned-one", refused)

    without = await MCPServerSnapshot.from_manager(manager, "banned-one")
    with_it = await MCPServerSnapshot.from_manager(manager, "banned-one", failures=failures)

    assert without.error == ""
    assert with_it.error == refused.reason


async def test_a_server_the_policy_denies_is_marked_blocked(manager) -> None:
    assert (await MCPServerSnapshot.from_manager(manager, "banned-one")).policy_blocked is True
    assert (await MCPServerSnapshot.from_manager(manager, "idle")).policy_blocked is False


async def test_the_summary_says_only_whether_a_server_is_connected(manager) -> None:
    assert (await manager.connect("notes")).ok

    assert MCPServerSummary.from_manager(manager, "notes").model_dump() == {
        "name": "notes",
        "connected": True,
    }
    assert MCPServerSummary.from_manager(manager, "idle").model_dump() == {
        "name": "idle",
        "connected": False,
    }


def test_a_toggle_result_echoes_the_tools_new_state() -> None:
    result = MCPToolToggleResult(server="notes", tool="delete_note", enabled=False)

    assert result.model_dump() == {"server": "notes", "tool": "delete_note", "enabled": False}


def test_a_snapshot_given_only_what_it_must_have_defaults_to_empty_and_unblocked() -> None:
    snap = MCPServerSnapshot(name="só", connected=False, transport="stdio")

    assert snap.model_dump() == {
        "name": "só",
        "connected": False,
        "transport": "stdio",
        "tool_names": [],
        "tool_descriptions": {},
        "tools_disabled": [],
        "resources": [],
        "prompts": [],
        "error": "",
        "policy_blocked": False,
    }


def test_every_field_of_every_mcp_model_is_documented_for_the_sdks() -> None:
    for model in (MCPToolToggleResult, MCPServerSummary, MCPServerSnapshot):
        schema = model.model_json_schema()
        assert schema["description"], model.__name__
        assert all(p.get("description") for p in schema["properties"].values()), model.__name__
