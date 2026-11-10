"""The project the session runs in — its directory, its policy, its rules.

Three things hang off the project directory: where the group's own
files live (the policy layout), what the project scaffold writes on
first run (the initializer), and the instructions and rules the tools
read (the project context).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.init import InitConfig, ProjectInitializer
from igni_core.core.utils.context import RulesContextLoader
from igni_core.core.utils.rules_index import RulesIndex
from igni_core.core.workspace_trust import process_trust

if TYPE_CHECKING:
    from igni_core.core.session.core import Session


class ProjectBuilder:
    """The project the session runs in — its policy, its scaffold, its rules."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def build_group_policy(self) -> None:
        """Where the group-policy cache keeps each kind's entries."""
        session = self._session
        session.group_policy = GroupPolicyLayout.for_data_dir(session.settings.storage.data_dir)

    def run_initializer(self) -> None:
        """Scaffold the project's own files, honouring what the group ships.

        The bundled agents stand down when the group ships its own —
        otherwise this would scaffold back the very agents an admin
        removed, and two sources would fight over one checksum file.
        """
        session = self._session
        ProjectInitializer(
            project_dir=session.project_dir,
            trust=process_trust(),
            config=InitConfig(
                skip_bundled_agents=session.group_policy.ships("agents"),
                skip_builtin_hook_registration=session.group_policy.ships("hooks"),
                group_ships_hook_scripts=session.group_policy.ships("scripts"),
            ),
        ).run()

    def build_context(self) -> None:
        """Top-level project instructions + the :class:`RulesIndex`.

        Subdirectory rules are discovered lazily by :class:`ToolEventHook`.
        """
        session = self._session
        settings = session.settings
        session.project_instructions = (
            RulesContextLoader(
                project_dir=session.project_dir,
                read_claude_md=settings.rules.cross_tool_support,
            )
            .load_all()
            .render()
        )
        session.rules_index = RulesIndex(
            session.project_dir,
            read_claude_md=settings.rules.cross_tool_support,
            group_rules_dir=session.group_policy.shipped_dir("rules"),
        )


__all__ = ["ProjectBuilder"]
