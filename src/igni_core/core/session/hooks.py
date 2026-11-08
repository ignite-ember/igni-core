"""The session's hooks: loader, registry, executor and group-hook gate."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from igni_core.core.config.settings import Settings
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.hooks.group_hook_gate import AskAboutGroupHook, GroupHookGate
from igni_core.core.hooks.loader import HookLoader
from igni_core.core.hooks.registry import HookRegistry
from igni_core.core.hooks.schemas import HookDefinition, HookLoadWarning
from igni_core.core.network_policy import NetworkPolicy
from igni_core.core.plugins import PluginLoader
from igni_core.core.session.mcp_resolver import MCPToolResolver
from igni_core.core.session.reminders import PendingReminderQueue

logger = logging.getLogger(__name__)


class SessionHooks:
    """Single owner of hook state; :class:`HookExecutor` is built only here.

    One per session. :meth:`load` runs at boot and on every plugin reload,
    :meth:`reload` on ``/hooks reload``; both go through :meth:`_build`, so
    plugin hooks survive either.
    """

    def __init__(
        self,
        *,
        project_dir: Path,
        settings: Settings,
        group_hooks_dir: Path,
        reminders: PendingReminderQueue,
        resolver: MCPToolResolver,
        ask: AskAboutGroupHook,
    ) -> None:
        self._project_dir = project_dir
        self._settings = settings
        self._group_hooks_dir = group_hooks_dir
        self._reminders = reminders
        self.resolver = resolver
        # One per session, so an answer given once is not asked again while
        # it runs; the mode is read from the settings of the latest load.
        self._gate = GroupHookGate(mode=lambda: self._settings.hooks.group, ask=ask)
        self._plugin_loader: PluginLoader | None = None
        self._disabled: set[str] = set()
        self._loader: HookLoader | None = None
        self._registry = HookRegistry.from_empty()
        self._executor = self._build_executor()

    @property
    def executor(self) -> HookExecutor:
        return self._executor

    @property
    def hooks_map(self) -> dict[str, list[HookDefinition]]:
        """The raw event map, shared by identity with the executor."""
        return self._registry.raw

    @property
    def gate(self) -> GroupHookGate:
        return self._gate

    def load(self, settings: Settings, plugin_loader: PluginLoader, disabled: set[str]) -> int:
        """Build hooks from the settings files plus enabled plugins."""
        self._settings = settings
        self._plugin_loader = plugin_loader
        self._disabled = disabled
        self._loader = HookLoader(
            self._project_dir,
            cross_tool_support=settings.hooks.cross_tool_support,
            group_dir=self._group_hooks_dir,
        )
        return self._build("hook load warning")

    def reload(self) -> int:
        """Re-read the settings files, keeping the last load's plugins."""
        return self._build("hook reload warning")

    def _build(self, warning_prefix: str) -> int:
        if self._loader is None:
            return 0
        load_result = self._loader.load()
        self._registry = load_result.registry
        warnings = list(load_result.warnings)
        if self._plugin_loader is not None:
            # Plugins prepend per event so project hooks still run last.
            plugin_result = self._plugin_loader.apply_to_hooks(
                self._loader, self._registry, disabled=self._disabled
            )
            warnings.extend(plugin_result.warnings)
        self._log_warnings(warning_prefix, warnings)
        # A reminder queued by the previous executor must not leak into this one.
        self._reminders.replace([])
        self._executor = self._build_executor()
        return self._registry.total_hooks

    def _build_executor(self) -> HookExecutor:
        return HookExecutor(
            self._registry.raw,
            mcp_resolver=self.resolver.resolve,
            rewake_callback=self._reminders.queue,
            network_policy=NetworkPolicy.from_settings(self._settings),
            group_hook_gate=self._gate,
        )

    @staticmethod
    def _log_warnings(prefix: str, warnings: Iterable[HookLoadWarning]) -> None:
        for warning in warnings:
            logger.warning(
                "%s [%s] from %s: %s", prefix, warning.kind, warning.source, warning.detail
            )


__all__ = ["SessionHooks"]
