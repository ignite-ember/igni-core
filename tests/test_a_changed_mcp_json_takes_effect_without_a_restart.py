"""A ``.mcp.json`` that changed while igni was up.

The configured servers used to be a one-shot snapshot taken when the
manager was built, so a ``.mcp.json`` added or edited after the backend
started was not seen until a restart, and the person had to restart
igni to get the server their repository declared (campaign R209/R669).

Now the config files are re-read: the changed servers are connected
again, the ones that are gone are dropped, and the team is rebuilt so
the tools follow.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.core.mcp.client import MCPClientManager
from igni_core.core.mcp.config import MCPPolicy, MCPServerConfig
from igni_core.core.mcp.schemas import MCPConnectResult
from igni_core.core.session.mcp_ops import McpFailureCache
from igni_core.core.session.startup import McpInitDeps, McpInitPhase


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / ".igni").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def _write(path: Path, servers: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return path


async def test_a_server_added_to_the_config_file_appears_without_a_restart(
    home: Path, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    manager = MCPClientManager(project_dir=project, policy=MCPPolicy())
    assert manager.list_servers() == []

    _write(project / ".mcp.json", {"late": {"command": "c"}})

    changed = await manager.reload_configs()

    assert changed == ["late"]
    assert manager.list_servers() == ["late"]
    assert manager.configs["late"].command == "c"


async def test_an_edited_server_is_reconnected_with_what_the_file_now_says(
    home: Path, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    _write(project / ".mcp.json", {"srv": {"command": "old"}})
    manager = MCPClientManager(project_dir=project, policy=MCPPolicy())
    manager._clients["srv"] = MagicMock()

    _write(project / ".mcp.json", {"srv": {"command": "new"}})

    changed = await manager.reload_configs()

    assert changed == ["srv"]
    assert manager.configs["srv"].command == "new"
    assert manager.list_connected() == [], "the old process is dropped, not reused"


async def test_a_server_taken_out_of_the_config_file_is_dropped(home: Path, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _write(project / ".mcp.json", {"gone": {"command": "c"}, "kept": {"command": "c"}})
    manager = MCPClientManager(project_dir=project, policy=MCPPolicy())
    manager._clients["gone"] = MagicMock()
    manager._clients["kept"] = MagicMock()

    _write(project / ".mcp.json", {"kept": {"command": "c"}})

    changed = await manager.reload_configs()

    assert changed == ["gone"]
    assert manager.list_servers() == ["kept"]
    assert manager.list_connected() == ["kept"]


async def test_a_plugin_s_server_and_an_extension_s_service_survive_a_reload(
    home: Path, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    _write(project / ".mcp.json", {"srv": {"command": "c"}})
    manager = MCPClientManager(project_dir=project, policy=MCPPolicy())
    manager.configs["acme:srv"] = MCPServerConfig(name="acme:srv", source="plugin")
    manager.register_server(MCPServerConfig(name="ext", source="extension"))

    _write(project / ".mcp.json", {"srv": {"command": "c2"}})

    await manager.reload_configs()

    assert manager.configs["acme:srv"].source == "plugin"
    assert manager.configs["ext"].source == "extension"
    assert manager.configs["srv"].command == "c2"


async def test_nothing_changed_means_no_work(home: Path, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _write(project / ".mcp.json", {"srv": {"command": "c"}})
    manager = MCPClientManager(project_dir=project, policy=MCPPolicy())

    assert await manager.reload_configs() == []


async def test_a_broken_config_file_leaves_the_servers_alone(home: Path, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _write(project / ".mcp.json", {"srv": {"command": "c"}})
    manager = MCPClientManager(project_dir=project, policy=MCPPolicy())

    (project / ".mcp.json").write_text("{not json", encoding="utf-8")

    assert await manager.reload_configs() == []
    assert manager.list_servers() == ["srv"]


def _phase(manager):
    pool = SimpleNamespace(build_agents=MagicMock())
    deps = McpInitDeps(
        mcp_manager=manager,
        failures=McpFailureCache(),
        display=MagicMock(),
        pool_ref=lambda: pool,
        rebuild_team=MagicMock(),
    )
    return McpInitPhase(deps), deps, pool


def _manager(project: Path) -> MCPClientManager:
    """A real manager over *project*, with the connect stubbed out.

    The stub is the transport: what is under test is which servers the
    manager knows about and when the phase rebuilds around them.
    """
    manager = MCPClientManager(project_dir=project, policy=MCPPolicy())
    manager.connect = AsyncMock(side_effect=lambda name: MCPConnectResult.success(MagicMock()))
    return manager


def _stub_manager(**extra):
    return SimpleNamespace(
        list_servers=lambda: ["srv"],
        list_connected=lambda: [],
        connect=AsyncMock(side_effect=lambda name: MCPConnectResult.success(MagicMock())),
        reload_configs=AsyncMock(return_value=[]),
        **extra,
    )


async def test_the_next_message_picks_up_a_changed_config(home: Path, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _write(project / ".mcp.json", {"srv": {"command": "c"}})
    manager = _manager(project)
    phase, _, pool = _phase(manager)

    await phase.ensure()
    assert pool.build_agents.call_count == 1

    _write(project / ".mcp.json", {"srv": {"command": "c2"}})

    result = await phase.ensure()

    assert result.rebuilt is True
    assert result.skipped_reason is None
    assert pool.build_agents.call_count == 2


async def test_an_unchanged_config_makes_the_next_message_a_no_op(
    home: Path, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    _write(project / ".mcp.json", {"srv": {"command": "c"}})
    manager = _manager(project)
    phase, _, pool = _phase(manager)

    await phase.ensure()
    result = await phase.ensure()

    assert result.skipped_reason == "already_initialized"
    assert pool.build_agents.call_count == 1


async def test_the_team_drops_the_tools_of_a_server_that_left_the_config(
    home: Path, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    _write(project / ".mcp.json", {"srv": {"command": "c"}})
    manager = _manager(project)
    phase, _, pool = _phase(manager)

    await phase.ensure()

    (project / ".mcp.json").unlink()
    result = await phase.ensure()

    assert result.rebuilt is True
    pool.build_agents.assert_called_with(mcp_clients={})


async def test_a_manager_without_a_reload_is_left_alone(home: Path, tmp_path: Path) -> None:
    """A manager stub from before the reload existed must not break the phase."""
    manager = SimpleNamespace(
        list_servers=lambda: ["srv"],
        list_connected=lambda: [],
        connect=AsyncMock(side_effect=lambda name: MCPConnectResult.success(MagicMock())),
    )
    phase, _, pool = _phase(manager)  # type: ignore[arg-type]

    await phase.ensure()
    result = await phase.ensure()

    assert result.skipped_reason == "already_initialized"
    assert pool.build_agents.call_count == 1
