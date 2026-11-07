"""Per-panel controllers for the FE panels concern.

The old :class:`PanelsController` in :mod:`server_panels` mixed
five distinct concerns (agents, hooks, skills, slash commands,
output styles) into one class. This subpackage decomposes each
into a focused single-responsibility controller, all sharing only
a :class:`Session` handle. The top-level
:class:`server_panels.PanelsController` retains its shape as a
thin composition facade so ``BackendServer.panels.<x>()`` and the
RPC router keep working.

Downstream code that wants direct access to one controller (e.g.
future per-panel unit tests) can import from here:

    from igni_core.backend.panels import HooksPanelController
"""

from igni_core.backend.panels.agents_panel import AgentsPanelController
from igni_core.backend.panels.hooks_panel import HooksPanelController
from igni_core.backend.panels.output_styles_catalog import OutputStylesCatalog
from igni_core.backend.panels.skills_panel import SkillsPanelController
from igni_core.backend.panels.slash_commands_catalog import SlashCommandsCatalog

__all__ = [
    "AgentsPanelController",
    "HooksPanelController",
    "OutputStylesCatalog",
    "SkillsPanelController",
    "SlashCommandsCatalog",
]
