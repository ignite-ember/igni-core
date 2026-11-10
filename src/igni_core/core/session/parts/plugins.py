"""Plugins, output styles, hooks — what a plugin contributes to the session.

A plugin is a directory of bundled skills, agents, hooks, MCP servers
and output styles. This module discovers them, loads the hooks, and
builds the tool-hook factory that carries a plugin's rules into every
tool call.

The plugin reload orchestrator lives here too: hot-reloading a plugin
is re-running this module's builders, so it belongs beside them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from igni_core.core.config.models import ModelRegistry
from igni_core.core.mcp.hints import ToolHintsRegistry
from igni_core.core.output_styles import discover_output_styles
from igni_core.core.plugins import PluginLoader, load_state
from igni_core.core.session.hooks import SessionHooks
from igni_core.core.session.mcp_resolver import MCPToolResolver
from igni_core.core.session.parts.agents import AgentsBuilder
from igni_core.core.session.plugin_reload import PluginReloadOrchestrator
from igni_core.core.session.tool_hook_factory import ToolEventHookFactory

if TYPE_CHECKING:
    from igni_core.core.session.core import Session


class PluginBuilder:
    """What a plugin contributes to the session — hooks, styles, tools."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def build_hooks(self) -> None:
        """The session's hooks: the loader, the executor and the group-hook gate."""
        session = self._session
        session.hooks = SessionHooks(
            project_dir=session.project_dir,
            settings=session.settings,
            group_hooks_dir=session.group_policy.dir_for("hooks"),
            reminders=session.reminders,
            resolver=MCPToolResolver(
                # The manager is built further down, after the hooks:
                # read it lazily, and read it as "absent" until it exists.
                mcp_manager_ref=lambda: getattr(session, "mcp_manager", None)
            ),
            ask=session.approval.ask,
        )

    def build_plugins_and_styles(self) -> None:
        """Plugins, then output styles, then hooks and the tool-hook factory.

        Also runs on every plugin hot-reload.
        """
        session = self._session
        settings = session.settings
        session.plugin_state = load_state(settings.storage.data_dir)
        session.plugin_loader = PluginLoader()
        session.plugin_loader.load_all(session.project_dir)
        managed_plugins = {p.name for p in session.plugin_loader.list_plugins() if p.is_managed}
        session.disabled_plugins = set(session.plugin_state.disabled) - managed_plugins

        plugin_style_roots = [
            (p.root_path, p.name)
            for p in session.plugin_loader.list_plugins()
            if p.name not in session.disabled_plugins
        ]
        session.output_style.adopt(
            discover_output_styles(
                session.project_dir,
                plugin_roots=plugin_style_roots,
                read_claude=settings.rules.cross_tool_support,
                group_dir=session.group_policy.shipped_dir("output-styles"),
            )
        )

        session.hooks.load(settings, session.plugin_loader, session.disabled_plugins)
        self.mirror_hooks()

        # Seeded with the live evaluator: an empty cache would rebuild it
        # from ``settings.permissions.mode`` and take the session out of
        # plan mode on ``/plugin install``.
        factory = ToolEventHookFactory(
            settings=settings,
            rules_index=session.rules_index,
            project_dir=session.project_dir,
            hooks=session.hooks,
            session_id_ref=lambda: session.session_id,
            permission_evaluator=session.permission_evaluator,
        )
        factory.bypass_guard = factory.build_bypass_guard(
            session.approval.ask,
            # Read per call: ``apply_workspace_trust`` replaces ``settings``.
            model_ref=lambda: ModelRegistry(session.settings).get_model(),
            # The process's registry, which the extension installer fills.
            tool_hints=ToolHintsRegistry.shared(),
        )
        factory.ask_person = session.approval.ask
        session.tool_hook_factory = factory
        session.permission_evaluator = factory.permission_evaluator

    def mirror_hooks(self) -> None:
        """Expose the current executor and hook map on the session."""
        session = self._session
        session.hook_executor = session.hooks.executor
        session.hooks_map = session.hooks.hooks_map

    def build_reload_orchestrator(self) -> None:
        """The hot-reload orchestration for a live session.

        Re-running the builders above is what a reload does, so the
        callbacks point at them rather than at a second copy of the logic.
        """
        session = self._session
        session.plugin_reload_orchestrator = PluginReloadOrchestrator(
            project_dir=session.project_dir,
            mcp_manager=session.mcp_manager,
            plugin_loader_ref=lambda: session.plugin_loader,
            disabled_plugins_ref=lambda: session.disabled_plugins,
            rebuild_plugins_and_hooks=lambda: PluginBuilder(session).build_plugins_and_styles(),
            rebuild_agent_and_skill_pools=lambda: AgentsBuilder(session).build_pools(),
            skill_pool_ref=lambda: session.skill_pool,
            agent_pool_ref=lambda: session.pool,
            hooks_map_ref=lambda: session.hooks.hooks_map,
            disconnect_removed_mcps=session.mcp_lifecycle.disconnect,
            auto_connect_mcps=session.mcp_lifecycle.connect,
        )


__all__ = ["PluginBuilder"]
