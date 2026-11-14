"""The tools builder passes the bag's choices on to the registry and loaders.

Its order, deduplication and gating are pinned beside the bag itself; what
is pinned here is what those tests leave open: the reasoning toolkit is
attached when the factory returns one, the shared process supervisor
reaches the registry, the disabled plugins reach the plugin loader, and a
custom loader that finds nothing adds nothing.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agno.tools import Toolkit

from igni_core.core.agents import AgentPool
from igni_core.core.config.settings import Settings
from igni_core.core.config.tool_permissions import ToolPermissions
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.mcp.client import MCPClientManager
from igni_core.core.plugins import PluginLoader
from igni_core.core.session.agent_builder.tools_builder import ToolsBuilder
from igni_core.core.session.agent_builder.tools_builder_deps import (
    CloudCredentials,
    OrchestrationDeps,
    SessionToolkits,
    ToolBuildDeps,
    ToolRegistryDeps,
    ToolSourceDeps,
)
from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator
from igni_core.core.tools.custom_loader_schemas import DiscoveryResult
from igni_core.core.tools.orchestrate_budget import SpawnBudget
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.tools.registry import ToolRegistry


class _Registry(ToolRegistry):
    built: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        type(self).built.append(kwargs)

    def resolve(self, tool_names):
        return []

    def load_custom_tools(self, project_dir=None, *, plugin_tool_dirs=None, group_tools_dir=None):
        return DiscoveryResult(toolkits=[])


class _Plugins(PluginLoader):
    def __init__(self) -> None:
        super().__init__(Path("/nonexistent"))
        self.disabled_seen: list[Any] = []

    def collect_tool_dirs(self, disabled=None):
        self.disabled_seen.append(disabled)
        return []


class _Mcp(MCPClientManager):
    def __init__(self) -> None:
        super().__init__(None)

    def list_connected(self) -> list[str]:
        return []


def _bag(
    tmp_path: Path,
    *,
    reasoning: Toolkit | None,
    plugins: _Plugins,
    supervisor: ProcessSupervisor,
) -> ToolBuildDeps:
    _Registry.built.clear()
    return ToolBuildDeps(
        registry=ToolRegistryDeps(
            project_dir=tmp_path,
            settings=Settings(),
            permissions_cls=ToolPermissions,
            registry_cls=_Registry,
            cloud=CloudCredentials(),
            broadcast=lambda event: None,
            supervisor=supervisor,
        ),
        sources=ToolSourceDeps(
            plugin_loader=plugins, disabled_plugins={"off-1", "off-2"}, group_tools_dir=None
        ),
        orchestration=OrchestrationDeps(
            pool=AgentPool(),
            hook_executor=HookExecutor({}),
            session_id="s",
            spawn_budget=SpawnBudget(2),
            sub_agent_hitl=SubAgentHITLCoordinator(),
        ),
        mcp_manager=_Mcp(),
        lsp_manager=None,
        monitor_manager=None,
        reasoning_factory=lambda settings: reasoning,
        resolve_tool_names=lambda registry: [],
        session_toolkits=SessionToolkits.for_session(SimpleNamespace()),
    )


def test_the_reasoning_toolkit_is_attached_when_the_factory_returns_one(tmp_path):
    marker = Toolkit(name="reasoning-marker")
    bag = _bag(tmp_path, reasoning=marker, plugins=_Plugins(), supervisor=ProcessSupervisor())
    assert marker in ToolsBuilder(bag).assemble()
    assert ToolsBuilder(bag).reasoning() == [marker]


def test_no_reasoning_toolkit_is_attached_when_the_factory_returns_none(tmp_path):
    bag = _bag(tmp_path, reasoning=None, plugins=_Plugins(), supervisor=ProcessSupervisor())
    assert ToolsBuilder(bag).reasoning() == []


def test_the_registry_runs_on_the_supervisor_the_session_shares(tmp_path):
    supervisor = ProcessSupervisor()
    bag = _bag(tmp_path, reasoning=None, plugins=_Plugins(), supervisor=supervisor)
    ToolsBuilder(bag).core_tools()
    [kwargs] = _Registry.built
    assert kwargs["supervisor"] is supervisor


def test_the_disabled_plugins_reach_the_plugin_loader(tmp_path):
    plugins = _Plugins()
    bag = _bag(tmp_path, reasoning=None, plugins=plugins, supervisor=ProcessSupervisor())
    ToolsBuilder(bag).custom()
    assert plugins.disabled_seen == [{"off-1", "off-2"}]


def test_a_custom_loader_that_finds_nothing_adds_no_toolkit(tmp_path):
    bag = _bag(tmp_path, reasoning=None, plugins=_Plugins(), supervisor=ProcessSupervisor())
    assert ToolsBuilder(bag).custom() == []
