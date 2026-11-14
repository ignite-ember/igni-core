"""What a session builds from its plugins comes from the enabled ones only.

A plugin the person disabled keeps its place in the list but contributes
nothing: no MCP server, no language server, no monitor, no output style.
A managed plugin cannot be disabled, so a state file that lists one
changes nothing. The project's own monitors run only in a folder the
person trusts, and the MCP manager is built with the group's MCP
directory and the deployment's network policy.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.session.core import Session
from igni_core.core.session.parts.tooling import ToolingBuilder
from igni_core.core.workspace_trust import WorkspaceTrust
from tests.session_factory import stub_project, stub_settings
from tests.session_plugin_rig import disable_plugin, write_plugin


def _session(root: Path, *, plugins=(), disabled=(), edit=None, group_mcp=None) -> Session:
    project = stub_project(root / "proj")
    for name in plugins:
        write_plugin(project, name)
    settings = stub_settings(project, home=root / "home")
    if edit is not None:
        edit(settings)
    if group_mcp is not None:
        mcps = GroupPolicyLayout.for_data_dir(settings.storage.data_dir).ensure_dir("mcps")
        (mcps / "shared.json").write_text(json.dumps({"mcpServers": group_mcp}))
    if disabled:
        disable_plugin(Path(settings.storage.data_dir), *disabled)
    return Session(settings=settings, project_dir=project)


def test_an_enabled_plugin_contributes_to_every_surface(tmp_path: Path) -> None:
    session = _session(tmp_path, plugins=("alpha",))

    assert list(session.mcp_manager.configs) == ["alpha:srv"]
    assert session.lsp_manager.list_servers() == ["alpha:ls"]
    assert list(session.monitor_manager._configs) == ["alpha:mon"]
    assert set(session.output_style.styles) == {"alpha-style"}


def test_a_disabled_plugin_contributes_to_none(tmp_path: Path) -> None:
    session = _session(tmp_path, plugins=("alpha", "beta"), disabled=("beta",))

    assert session.disabled_plugins == {"beta"}
    assert list(session.mcp_manager.configs) == ["alpha:srv"]
    assert session.lsp_manager.list_servers() == ["alpha:ls"]
    assert list(session.monitor_manager._configs) == ["alpha:mon"]
    assert set(session.output_style.styles) == {"alpha-style"}


def test_a_managed_plugin_listed_as_disabled_stays_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed = tmp_path / "managed"
    gamma = write_plugin(tmp_path, "gamma", plugins_root=managed / CONFIG_DIR / "plugins")
    # A managed plugin's agents are not loadable yet (see the agent loader's priorities).
    shutil.rmtree(gamma / "agents")
    monkeypatch.setattr(
        "igni_core.core.plugins.loader._platform_managed_plugins_root", lambda: managed
    )

    session = _session(tmp_path, plugins=("alpha",), disabled=("gamma", "alpha"))

    assert session.disabled_plugins == {"alpha"}
    assert list(session.mcp_manager.configs) == ["gamma:srv"]
    assert "gamma-style" in session.output_style.styles


def test_rebuilding_the_tooling_replaces_the_managers_with_the_current_plugin_set(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path, plugins=("alpha",))
    old_lsp, old_monitors = session.lsp_manager, session.monitor_manager

    session.disabled_plugins = {"alpha"}
    ToolingBuilder(session).build()

    assert session.lsp_manager is not old_lsp
    assert session.monitor_manager is not old_monitors
    assert session.lsp_manager.list_servers() == []
    assert list(session.monitor_manager._configs) == []


def _project_monitor(root: Path) -> None:
    (root / "proj" / ".monitors.json").write_text(
        json.dumps({"monitors": {"mine": {"command": "echo"}}})
    )


def test_the_project_s_own_monitors_run_in_a_trusted_folder(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _project_monitor(tmp_path)

    ToolingBuilder(session).build()

    assert list(session.monitor_manager._configs) == ["mine"]


def test_the_project_s_own_monitors_wait_for_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path)
    _project_monitor(tmp_path)
    monkeypatch.setattr(
        "igni_core.core.session.parts.tooling.process_trust", lambda: WorkspaceTrust()
    )

    ToolingBuilder(session).build()

    assert list(session.monitor_manager._configs) == []


def test_the_mcp_manager_reads_the_group_s_mcp_directory(tmp_path: Path) -> None:
    session = _session(tmp_path, group_mcp={"shared": {"command": "echo"}})

    assert session.mcp_manager.configs["shared"].source == "group-policy"


def test_the_mcp_manager_carries_the_deployment_s_network_policy(tmp_path: Path) -> None:
    def air_gapped(settings) -> None:
        settings.air_gapped = True

    session = _session(tmp_path, edit=air_gapped)

    policy = session.mcp_manager._network_policy
    assert policy.air_gapped is True
    assert policy.allows("https://public.example.com/mcp") is False


def test_the_mcp_failure_cache_starts_empty(tmp_path: Path) -> None:
    session = _session(tmp_path)

    assert list(session.mcp_failures.items()) == []
    assert session.mcp_failures.reason("alpha:srv") == ""


async def test_the_lifecycle_rebuilds_the_team_through_the_session_s_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path)
    rebuilt: list[str] = []

    async def disconnected(name: str) -> bool:
        return True

    monkeypatch.setattr(session.mcp_manager, "disconnect_one", disconnected)
    monkeypatch.setattr(session.startup, "rebuild_mcp", lambda: rebuilt.append("rebuilt"))

    await session.mcp_lifecycle.disconnect({"alpha:srv"})

    assert rebuilt == ["rebuilt"]
