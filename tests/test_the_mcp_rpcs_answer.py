"""The MCP RPCs hand the call to the session's MCP machinery and answer
what it says, through the real MCP controller.

A server is named by ``server_name`` and a tool switched by ``server``,
``tool`` and ``enabled``; a call missing one is refused before anything
is touched, and keys a newer client adds are ignored. Switching a tool
is kept in the project's ``.igni/mcp-tool-state.json`` and fails when
that file cannot be written, rather than answering as if it had been.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.backend.rpc_binding import RpcArgsError
from igni_core.backend.rpc_handlers.mcp import McpRpcHandler, McpServerArgs, McpToolEnabledArgs
from igni_core.backend.schemas_mcp import MCPToolToggleResult
from igni_core.backend.server_mcp import McpController
from igni_core.core.mcp.client import MCPClientManager
from igni_core.core.mcp.schemas import MCPConnectResult
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.session.schemas import McpInitResult, McpServerStatus
from igni_core.protocol.rpc import RpcMethod


def _handler(session: SimpleNamespace) -> McpRpcHandler:
    controller = McpController(session)  # type: ignore[arg-type]
    backend = SimpleNamespace(
        controllers=SimpleNamespace(mcp=controller),
        mcp_connect=controller.connect,
        get_mcp_status=controller.status,
        get_mcp_servers=controller.servers,
        get_mcp_server_details=controller.server_details,
    )
    return McpRpcHandler(SimpleNamespace(backend=backend))  # type: ignore[arg-type]


def _session(manager: object | None = None, **more: object) -> SimpleNamespace:
    return SimpleNamespace(
        mcp_manager=manager or MagicMock(),
        mcp_failures=MagicMock(),
        startup=SimpleNamespace(rebuild_mcp=MagicMock(), ensure_mcp=AsyncMock()),
        mcp_lifecycle=SimpleNamespace(status=MagicMock(return_value=[])),
        **more,
    )


def _manager(project: Path) -> MCPClientManager:
    return MCPClientManager(project_dir=project)


def _state(project: Path) -> dict:
    return json.loads((project / CONFIG_DIR / "mcp-tool-state.json").read_text())


async def test_ensure_answers_what_connecting_the_servers_came_to() -> None:
    session = _session()
    result = McpInitResult(connected=["github"], failed={"jira": "refused"}, rebuilt=True)
    session.startup.ensure_mcp.return_value = result

    assert await _handler(session).methods()[RpcMethod.ENSURE_MCP]({}) == result


async def test_connecting_a_server_names_the_one_asked_for() -> None:
    manager = MagicMock()
    manager.connect = AsyncMock(return_value=MCPConnectResult.success(MagicMock()))
    session = _session(manager)

    info = await _handler(session).methods()[RpcMethod.MCP_CONNECT]({"server_name": "github"})

    assert info.text == "Connected MCP: github"
    manager.connect.assert_awaited_once_with("github")


async def test_a_server_that_would_not_connect_fails_the_call_with_the_reason() -> None:
    manager = MagicMock()
    manager.connect = AsyncMock(return_value=MCPConnectResult.failure("refused by policy"))

    with pytest.raises(ValueError, match="Could not connect MCP github: refused by policy"):
        await _handler(_session(manager)).methods()[RpcMethod.MCP_CONNECT](
            {"server_name": "github"}
        )


async def test_disconnecting_a_server_names_the_one_asked_for() -> None:
    manager = MagicMock()
    manager.disconnect_one = AsyncMock(return_value=False)
    session = _session(manager)

    info = await _handler(session).methods()[RpcMethod.MCP_DISCONNECT]({"server_name": "github"})

    assert info.text == "Disconnected MCP: github"
    manager.disconnect_one.assert_awaited_once_with("github")
    session.startup.rebuild_mcp.assert_called_once_with()


@pytest.mark.parametrize("method", [RpcMethod.MCP_CONNECT, RpcMethod.MCP_DISCONNECT])
async def test_a_call_without_a_server_name_is_refused(method: RpcMethod) -> None:
    manager = MagicMock()
    manager.connect = AsyncMock()
    manager.disconnect_one = AsyncMock()

    with pytest.raises(RpcArgsError, match="server_name"):
        await _handler(_session(manager)).methods()[method]({"name": "github"})

    manager.connect.assert_not_awaited()
    manager.disconnect_one.assert_not_awaited()


def test_the_status_is_the_sessions_one_row_per_server() -> None:
    session = _session()
    rows = [
        McpServerStatus(name="github", connected=True),
        McpServerStatus(name="jira", connected=False),
    ]
    session.mcp_lifecycle.status.return_value = rows

    assert _handler(session).methods()[RpcMethod.GET_MCP_STATUS]({}) == rows


def test_the_configured_servers_are_listed_in_configuration_order_with_their_state() -> None:
    manager = SimpleNamespace(
        list_servers=lambda: ["zeta", "alpha", "mid"], list_connected=lambda: ["alpha"]
    )

    rows = _handler(_session(manager)).methods()[RpcMethod.GET_MCP_SERVERS]({})

    assert [(r.name, r.connected) for r in rows] == [
        ("zeta", False),
        ("alpha", True),
        ("mid", False),
    ]


async def test_a_server_that_is_not_configured_gets_no_details() -> None:
    manager = SimpleNamespace(list_servers=lambda: [])

    assert await _handler(_session(manager)).methods()[RpcMethod.GET_MCP_SERVER_DETAILS]({}) == []


async def test_the_details_describe_each_server_in_turn() -> None:
    manager = SimpleNamespace(
        list_servers=lambda: ["a", "b"],
        list_connected=lambda: ["b"],
        configs={},
        get_tools=lambda name: [f"{name}_tool"],
        get_tool_descriptions=lambda name: {f"{name}_tool": "does it"},
        get_disabled_tools=lambda name: [],
        get_resources=AsyncMock(return_value=[]),
        get_prompts=AsyncMock(return_value=[]),
        is_policy_denied=lambda name: name == "a",
    )
    session = _session(manager)
    session.mcp_failures = SimpleNamespace(reason=lambda name: f"{name} said no")

    rows = await _handler(session).methods()[RpcMethod.GET_MCP_SERVER_DETAILS]({})

    assert [(r.name, r.connected, r.error, r.policy_blocked) for r in rows] == [
        ("a", False, "a said no", True),
        ("b", True, "b said no", False),
    ]
    assert rows[0].tool_names == ["a_tool"]


def test_switching_a_tool_off_is_kept_in_the_project(tmp_path: Path) -> None:
    session = _session(_manager(tmp_path))

    answer = _handler(session).methods()[RpcMethod.SET_MCP_TOOL_ENABLED](
        {"server": "github", "tool": "delete_repo", "enabled": False}
    )

    assert answer == MCPToolToggleResult(server="github", tool="delete_repo", enabled=False)
    assert _state(tmp_path) == {"disabled": {"github": ["delete_repo"]}}


def test_switching_a_tool_back_on_removes_it_from_the_project(tmp_path: Path) -> None:
    call = _handler(_session(_manager(tmp_path))).methods()[RpcMethod.SET_MCP_TOOL_ENABLED]
    call({"server": "github", "tool": "delete_repo", "enabled": False})
    call({"server": "github", "tool": "push", "enabled": False})

    answer = call({"server": "github", "tool": "delete_repo", "enabled": True})

    assert answer.enabled is True
    assert _state(tmp_path) == {"disabled": {"github": ["push"]}}


def test_a_tool_that_does_not_exist_is_accepted(tmp_path: Path) -> None:
    answer = _handler(_session(_manager(tmp_path))).methods()[RpcMethod.SET_MCP_TOOL_ENABLED](
        {"server": "nowhere", "tool": "nothing", "enabled": False}
    )

    assert (answer.server, answer.tool, answer.enabled) == ("nowhere", "nothing", False)


def test_a_choice_that_could_not_be_written_fails_the_call(tmp_path: Path) -> None:
    (tmp_path / CONFIG_DIR).write_text("a file where the folder should be")
    call = _handler(_session(_manager(tmp_path))).methods()[RpcMethod.SET_MCP_TOOL_ENABLED]

    with pytest.raises(OSError):
        call({"server": "github", "tool": "delete_repo", "enabled": False})


def test_a_choice_that_could_not_be_written_still_holds_for_this_session(tmp_path: Path) -> None:
    (tmp_path / CONFIG_DIR).write_text("a file where the folder should be")
    manager = _manager(tmp_path)
    live = SimpleNamespace(functions={"delete_repo": object(), "push": object()})
    manager._clients["github"] = live  # type: ignore[assignment]
    call = _handler(_session(manager)).methods()[RpcMethod.SET_MCP_TOOL_ENABLED]

    with pytest.raises(OSError):
        call({"server": "github", "tool": "delete_repo", "enabled": False})

    assert list(live.functions) == ["push"]


@pytest.mark.parametrize(
    "wire",
    [
        {"tool": "t", "enabled": True},
        {"server": "s", "enabled": True},
        {"server": "s", "tool": "t"},
        {"server": "s", "tool": "t", "enabled": "perhaps"},
        {"server_name": "s", "tool": "t", "enabled": True},
    ],
)
def test_a_toggle_missing_a_part_or_not_a_bool_is_refused(wire: dict, tmp_path: Path) -> None:
    call = _handler(_session(_manager(tmp_path))).methods()[RpcMethod.SET_MCP_TOOL_ENABLED]

    with pytest.raises(RpcArgsError):
        call(wire)

    assert not (tmp_path / CONFIG_DIR / "mcp-tool-state.json").exists()


def test_keys_a_newer_client_adds_are_ignored() -> None:
    assert McpServerArgs.model_validate({"server_name": "s", "later": 1}).server_name == "s"
    toggle = McpToolEnabledArgs.model_validate(
        {"server": "s", "tool": "t", "enabled": False, "later": 1}
    )
    assert (toggle.server, toggle.tool, toggle.enabled) == ("s", "t", False)
