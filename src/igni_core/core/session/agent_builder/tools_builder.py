"""Toolkit-assembly stage of the main-agent build.

Owns everything that ends up in the ``tools=[...]`` kwarg passed
to ``Agent(...)``. One method per toolkit family so each family is
testable in isolation and the assembly order is a single readable
sequence in :meth:`ToolsBuilder.assemble`.

The builder is session-free. Everything it needs arrives in one
:class:`ToolBuildDeps` bag, and the toolkits that answer back into a
live session — loop, todo, plan, slash — arrive already built, in
:class:`SessionToolkits`. The coordinator, which owns the session,
builds those and hands them over; this module never sees a
:class:`Session`.
"""

from __future__ import annotations

from agno.tools import Toolkit

from igni_core.core.tools.lsp import LspTools
from igni_core.core.tools.monitors import MonitorTools
from igni_core.core.tools.orchestrate import OrchestrateTools
from igni_core.core.tools.registry import ToolRegistry

from .tools_builder_deps import ToolBuildDeps


class ToolsBuilder:
    """Assemble the main agent's ``tools`` list.

    Each ``.<family>()`` method returns the toolkits it owns
    (usually 0-2 instances). :meth:`assemble` composes them in
    the same order the pre-refactor procedural version used —
    the ordering carried semantic meaning (custom tools last, so
    they can shadow built-ins if the plugin author wants; MCP
    clients after core toolkits so their tool names don't collide
    with same-named built-ins).
    """

    def __init__(self, deps: ToolBuildDeps) -> None:
        self._deps = deps
        self._registry: ToolRegistry | None = None

    def _build_registry(self) -> ToolRegistry:
        """Construct the tool registry once and cache it.

        The registry is shared between :meth:`core_tools` (name-
        based resolution) and :meth:`custom` (custom-toolkit
        loading), so we build it lazily on first access.
        """
        if self._registry is None:
            deps = self._deps.registry
            self._registry = deps.registry_cls(
                base_dir=str(deps.project_dir),
                permissions=deps.permissions_cls(
                    project_dir=deps.project_dir,
                    settings_permissions=deps.settings.permissions,
                ),
                cloud_token=deps.cloud.token,
                cloud_server_url=deps.cloud.server_url,
                broadcast=deps.broadcast,
                supervisor=deps.supervisor,
            )
        return self._registry

    def core_tools(self) -> list[Toolkit]:
        """Registry-resolved built-ins (Write / Edit / Bash / etc.)."""
        registry = self._build_registry()
        tool_names = self._deps.resolve_tool_names(registry)
        return list(registry.resolve(tool_names))

    def orchestrate(self) -> list[Toolkit]:
        """Delegation surface — spawn_agent / spawn_team."""
        orchestration = self._deps.orchestration
        registry = self._deps.registry
        return [
            OrchestrateTools(
                pool=orchestration.pool,
                settings=registry.settings,
                current_depth=0,
                hook_executor=orchestration.hook_executor,
                session_id=orchestration.session_id,
                hitl_coordinator=orchestration.sub_agent_hitl,
                project_dir=registry.project_dir,
                budget=orchestration.spawn_budget,
            )
        ]

    def reasoning(self) -> list[Toolkit]:
        """Optional Agno ReasoningTools — enabled by settings."""
        tools = self._deps.reasoning_factory(self._deps.registry.settings)
        return [tools] if tools else []

    def loop(self) -> list[Toolkit]:
        """/loop-driven iteration control + per-iter scratchpad."""
        toolkits = self._deps.session_toolkits
        return [toolkits.loop, toolkits.loop_progress]

    def todo(self) -> list[Toolkit]:
        """Agent-facing TodoWrite (CC parity)."""
        return [self._deps.session_toolkits.todo]

    def plan(self) -> list[Toolkit]:
        """Agent-facing exit_plan_mode (row 50)."""
        return [self._deps.session_toolkits.plan]

    def slash(self) -> list[Toolkit]:
        """Agent-facing re-entrant slash-command dispatch."""
        return [self._deps.session_toolkits.slash]

    def lsp(self) -> list[Toolkit]:
        """LSP query toolkit — only when at least one server exists."""
        manager = self._deps.lsp_manager
        if manager is None or not manager.list_servers():
            return []
        return [LspTools(manager)]

    def monitors(self) -> list[Toolkit]:
        """Monitor inspection toolkit — only when configured."""
        manager = self._deps.monitor_manager
        if manager is None or not manager.list_names():
            return []
        return [MonitorTools(manager)]

    def mcp(self, tools_so_far: list[Toolkit]) -> list[Toolkit]:
        """Connected MCP-server clients, deduped against ``tools_so_far``.

        The original code appended each client only if not already
        in the outbound list — preserved here so a client that
        also implements ``AgnoToolkit`` semantics and got pulled
        in earlier isn't attached twice.
        """
        manager = self._deps.mcp_manager
        extras: list[Toolkit] = []
        for name in manager.list_connected():
            client = manager.get_client(name)
            if client and client not in tools_so_far and client not in extras:
                extras.append(client)
        return extras

    def custom(self) -> list[Toolkit]:
        """Custom toolkits from ``.igni/tools/``, plugins, and the group."""
        registry = self._build_registry()
        sources = self._deps.sources
        plugin_tool_dirs = sources.plugin_loader.collect_tool_dirs(
            disabled=sources.disabled_plugins,
        )
        loaded = registry.load_custom_tools(
            self._deps.registry.project_dir,
            plugin_tool_dirs=plugin_tool_dirs,
            group_tools_dir=sources.group_tools_dir,
        )
        return list(loaded.toolkits)

    def assemble(self) -> list[Toolkit]:
        """Compose all toolkit families in the canonical order.

        Order preserved from the pre-refactor procedural version:
        core → orchestrate → reasoning → knowledge → loop → todo
        → plan → slash → lsp → monitors → mcp (dedup) → custom.
        """
        tools: list[Toolkit] = []
        tools.extend(self.core_tools())
        tools.extend(self.orchestrate())
        tools.extend(self.reasoning())
        tools.extend(self.loop())
        tools.extend(self.todo())
        tools.extend(self.plan())
        tools.extend(self.slash())
        tools.extend(self.lsp())
        tools.extend(self.monitors())
        tools.extend(self.mcp(tools))
        tools.extend(self.custom())
        return tools


__all__ = ["ToolsBuilder"]
