"""Which tier each MCP config file's servers are reported as.

The loader reads four kinds of file: the person's own
``~/.igni/.mcp.json``, the group's per-server files, the project's
``.mcp.json`` and the project's ``.igni/.mcp.json``. Every entry says
which of them it came from, because the MCP page uses that to decide
whether an approval can be withdrawn: a server the person's own config
declares is trusted for good, one a repository declares is not.

The project's file used to be read through the same call as the home
config and so took the ``"user"`` default: a repository's server looked
like the person's own, the page offered no Revoke for it, and the
project tier never reached the wire at all (campaign R209/R669).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from igni_core.backend.server_mcp import McpController
from igni_core.core.mcp.client import MCPClientManager
from igni_core.core.mcp.config import MCPConfigLoader, MCPPolicy
from igni_core.core.session.mcp_ops import (
    McpFailureCache,
    McpLifecycleCoordinator,
    McpLifecycleDeps,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / ".igni").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # The user-global config path is worked out when the module is
    # imported, so it has to be pointed at the fake home as well.
    monkeypatch.setattr(
        "igni_core.core.mcp.approval._USER_GLOBAL_MCP", str(home / ".igni" / ".mcp.json")
    )
    return home


def _write(path: Path, servers: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return path


def test_the_person_s_own_config_is_the_user_tier(home: Path, tmp_path: Path) -> None:
    _write(home / ".igni" / ".mcp.json", {"mine": {"command": "c"}})

    servers = MCPConfigLoader(project_dir=tmp_path / "proj").load()

    assert servers["mine"].source == "user"
    assert servers["mine"].source_path == str(home / ".igni" / ".mcp.json")


def test_the_projects_own_config_is_the_project_tier(home: Path, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _write(project / ".mcp.json", {"theirs": {"command": "c"}})

    servers = MCPConfigLoader(project_dir=project).load()

    assert servers["theirs"].source == "project"
    assert servers["theirs"].source_path == str(project / ".mcp.json")


def test_the_projects_igni_folder_is_the_project_tier_too(home: Path, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _write(project / ".igni" / ".mcp.json", {"theirs": {"command": "c"}})

    servers = MCPConfigLoader(project_dir=project).load()

    assert servers["theirs"].source == "project"
    assert servers["theirs"].source_path == str(project / ".igni" / ".mcp.json")


def test_the_group_s_files_are_the_group_policy_tier(home: Path, tmp_path: Path) -> None:
    group = tmp_path / "group"
    _write(group / "acme.json", {"theirs": {"command": "c"}})

    servers = MCPConfigLoader(project_dir=tmp_path / "proj", group_mcps_dir=group).load()

    assert servers["theirs"].source == "group-policy"


def test_a_plugin_s_own_file_is_the_plugin_tier(home: Path, tmp_path: Path) -> None:
    plugin = tmp_path / "plug"
    _write(plugin / ".mcp.json", {"srv": {"command": "c"}})

    servers: dict = {}
    MCPConfigLoader(project_dir=tmp_path / "proj").load_plugin_servers(plugin, "acme", servers)

    assert servers["acme:srv"].source == "plugin"


def _controller(manager: MCPClientManager) -> McpController:
    session = SimpleNamespace(
        mcp_manager=manager,
        mcp_failures=McpFailureCache(),
        startup=SimpleNamespace(rebuild_mcp=lambda: None, ensure_mcp=None),
        mcp_lifecycle=McpLifecycleCoordinator(
            McpLifecycleDeps(mcp_manager=manager, rebuild=lambda: None)
        ),
    )
    return McpController(session)  # type: ignore[arg-type]


async def test_the_mcp_page_is_told_a_project_server_is_the_projects(
    home: Path, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    _write(project / ".mcp.json", {"theirs": {"command": "c"}})
    manager = MCPClientManager(project_dir=project, policy=MCPPolicy())

    approvals = _controller(manager).approvals()

    assert [(a.name, a.source) for a in approvals] == [("theirs", "project")]


async def test_the_approval_of_a_project_server_can_be_withdrawn(
    home: Path, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    _write(project / ".mcp.json", {"theirs": {"command": "c"}})
    manager = MCPClientManager(project_dir=project, policy=MCPPolicy())
    controller = _controller(manager)

    approved = await controller.set_approval("theirs", True)
    assert approved.approved is True

    withdrawn = await controller.set_approval("theirs", False)
    assert withdrawn.approved is False


async def test_the_person_s_own_server_is_trusted_and_cannot_be_withdrawn(
    home: Path, tmp_path: Path
) -> None:
    _write(home / ".igni" / ".mcp.json", {"mine": {"command": "c"}})
    manager = MCPClientManager(project_dir=tmp_path / "proj", policy=MCPPolicy())
    controller = _controller(manager)

    approvals = _controller(manager).approvals()
    assert [(a.name, a.source, a.approved) for a in approvals] == [("mine", "user", True)]

    with pytest.raises(ValueError, match="cannot be withdrawn here"):
        await controller.set_approval("mine", False)
