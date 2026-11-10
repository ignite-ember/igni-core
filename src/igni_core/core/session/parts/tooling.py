"""The tooling the session runs alongside the agents.

Language servers (the ones the IDE would start) and the monitors
(bgproc-style watchers) are both read from the project's configuration
and the plugins' own bundled configurations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from igni_core.core.lsp import LspServerManager, load_lsp_config
from igni_core.core.monitors import MonitorConfigLoader, MonitorManager
from igni_core.core.workspace_trust import process_trust

if TYPE_CHECKING:
    from igni_core.core.session.core import Session


class ToolingBuilder:
    """The tooling the session runs alongside the agents."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def build(self) -> None:
        """The language-server manager (lazy) and monitor manager from the plugin set."""
        session = self._session
        lsp_configs = load_lsp_config(
            session.project_dir,
            plugin_roots=session.plugin_loader.collect_lsp_roots(disabled=session.disabled_plugins),
        )
        session.lsp_manager = LspServerManager(lsp_configs, session.project_dir)

        monitor_configs = MonitorConfigLoader(
            session.project_dir,
            trust=process_trust(),
            plugin_roots=session.plugin_loader.collect_monitor_roots(
                disabled=session.disabled_plugins
            ),
        ).load()
        session.monitor_manager = MonitorManager(monitor_configs, session.project_dir)


__all__ = ["ToolingBuilder"]
