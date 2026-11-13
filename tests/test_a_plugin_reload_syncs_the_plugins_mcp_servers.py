"""A plugin reload re-reads the plugins and syncs the MCP servers they own.

``PluginReloadOrchestrator.reload`` rebuilds the plugins and hooks, then
the agent and skill pools, then re-applies the plugins' MCP servers: a
server a plugin no longer contributes is disconnected, one it newly
contributes is connected, a server the user configured is left alone, and
the counts of what is loaded come back. The main team is not rebuilt here:
the session builds it once, after the reload.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.core.session.plugin_reload import PluginReloadOrchestrator
from igni_core.core.session.schemas import PluginReloadCounts


class FakePlugins:
    """A plugin loader: each enabled plugin contributes one MCP server."""

    def __init__(self, names: list[str]) -> None:
        self.names = names

    def list_plugins(self) -> list[Any]:
        return [SimpleNamespace(name=n) for n in self.names]

    def apply_to_mcp(self, _loader: Any, configs: dict, *, disabled: set[str]) -> None:
        for name in self.names:
            if name not in disabled:
                configs[f"{name}:server"] = {"command": name}


class Rig:
    def __init__(self, tmp_path: Path, plugins: list[str], disabled: set[str] | None = None):
        self.calls: list[str] = []
        self.disconnected: list[set[str]] = []
        self.connected: list[set[str]] = []
        self.coroutines: list[Any] = []
        self.plugins = FakePlugins(plugins)
        self.disabled = disabled or set()
        self.configs: dict[str, Any] = {}
        self.hooks: dict[str, list] = {}
        self.orchestrator = PluginReloadOrchestrator(
            project_dir=tmp_path,
            mcp_manager=SimpleNamespace(configs=self.configs),  # type: ignore[arg-type]
            plugin_loader_ref=lambda: self.plugins,  # type: ignore[arg-type,return-value]
            disabled_plugins_ref=lambda: self.disabled,
            rebuild_plugins_and_hooks=lambda: self.calls.append("plugins+hooks"),
            rebuild_agent_and_skill_pools=lambda: self.calls.append("pools"),
            skill_pool_ref=lambda: SimpleNamespace(list_skills=lambda: ["a", "b", "c"]),
            agent_pool_ref=lambda: SimpleNamespace(list_agents=lambda: ["x", "y"]),
            hooks_map_ref=lambda: self.hooks,
            disconnect_removed_mcps=self._disconnect,
            auto_connect_mcps=self._connect,
        )

    def _disconnect(self, names: set[str]):
        async def work() -> None:
            self.disconnected.append(names)

        coroutine = work()
        self.coroutines.append(coroutine)
        return coroutine

    def _connect(self, names: set[str]):
        async def work() -> None:
            self.connected.append(names)

        coroutine = work()
        self.coroutines.append(coroutine)
        return coroutine


async def _settle() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_a_reload_rebuilds_in_order_and_counts_what_is_loaded(tmp_path) -> None:
    rig = Rig(tmp_path, ["alpha", "beta"])
    rig.hooks = {"PreToolUse": [1, 2], "Stop": [3], "Empty": []}

    counts = rig.orchestrator.reload()

    assert rig.calls == ["plugins+hooks", "pools"]
    assert counts == PluginReloadCounts(plugins=2, skills=3, agents=2, hooks=3)


async def test_a_reload_with_nothing_installed_counts_zero(tmp_path) -> None:
    rig = Rig(tmp_path, [])
    rig.orchestrator._skill_pool_ref = lambda: SimpleNamespace(list_skills=lambda: [])
    rig.orchestrator._agent_pool_ref = lambda: SimpleNamespace(list_agents=lambda: [])

    counts = rig.orchestrator.reload()

    assert counts == PluginReloadCounts(plugins=0, skills=0, agents=0, hooks=0)
    assert rig.configs == {}


async def test_enabling_a_plugin_adds_and_connects_its_servers(tmp_path) -> None:
    rig = Rig(tmp_path, ["alpha"])

    rig.orchestrator.reload()
    await _settle()

    assert set(rig.configs) == {"alpha:server"}
    assert rig.connected == [{"alpha:server"}]
    assert rig.disconnected == []


async def test_disabling_a_plugin_drops_and_disconnects_its_servers(tmp_path) -> None:
    rig = Rig(tmp_path, ["alpha", "beta"])
    rig.orchestrator.reload()
    await _settle()
    rig.connected.clear()

    rig.disabled = {"alpha"}
    rig.orchestrator.reload()
    await _settle()

    assert set(rig.configs) == {"beta:server"}
    assert rig.disconnected == [{"alpha:server"}]
    assert rig.connected == [], "beta's server was already there"


async def test_a_reload_that_changes_nothing_connects_and_disconnects_nothing(tmp_path) -> None:
    rig = Rig(tmp_path, ["alpha"])
    rig.orchestrator.reload()
    await _settle()
    rig.connected.clear()

    rig.orchestrator.reload()
    await _settle()

    assert rig.connected == [] and rig.disconnected == []
    assert set(rig.configs) == {"alpha:server"}


async def test_a_server_the_user_configured_is_left_alone(tmp_path) -> None:
    rig = Rig(tmp_path, ["alpha"])
    rig.configs["mine"] = {"command": "mine"}
    # Shares the plugin's name but not its ``alpha:`` prefix.
    rig.configs["alphabet"] = {"command": "alphabet"}
    rig.disabled = {"alpha"}

    rig.orchestrator.reload()
    await _settle()

    assert set(rig.configs) == {"mine", "alphabet"}
    assert rig.disconnected == [] and rig.connected == []


async def test_a_plugin_server_that_vanished_is_the_only_one_dropped(tmp_path) -> None:
    rig = Rig(tmp_path, ["alpha"])
    rig.configs["alpha:old"] = {"command": "old"}
    rig.configs["mine"] = {"command": "mine"}

    rig.orchestrator.reload()
    await _settle()

    assert set(rig.configs) == {"mine", "alpha:server"}
    assert rig.disconnected == [{"alpha:old"}]
    assert rig.connected == [{"alpha:server"}]


def test_a_rebuild_that_fails_reaches_the_caller(tmp_path) -> None:
    rig = Rig(tmp_path, ["alpha"])

    def boom() -> None:
        raise RuntimeError("bad plugin")

    rig.orchestrator._rebuild_plugins_and_hooks = boom

    with pytest.raises(RuntimeError, match="bad plugin"):
        rig.orchestrator.reload()
    assert rig.configs == {}
