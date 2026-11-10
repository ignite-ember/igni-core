"""The agents — the pools the orchestrator draws on, and the main team.

The pools hold the definitions a plugin or the project ships; the main
team is the agent the session itself talks to. The team is built last
because it reads nearly everything else.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agno.agent import Agent

from igni_core.core.agents import AgentContributionApplier, AgentPool
from igni_core.core.agents.tool_check import UnresolvedAgentTools
from igni_core.core.config.models import ModelRegistry
from igni_core.core.config.tool_permissions import ToolPermissions
from igni_core.core.hooks.tool_hook import ToolEventHook
from igni_core.core.session.agent_builder import MainAgentBuilder
from igni_core.core.session.agent_factory import (
    create_guardrails,
    create_reasoning_tools,
)
from igni_core.core.session.compression import MeasuredCompression
from igni_core.core.skills.loader import SkillPool
from igni_core.core.tools.orchestrate_budget import SpawnBudget
from igni_core.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from igni_core.core.session.core import Session

logger = logging.getLogger(__name__)


class AgentsBuilder:
    """The pools the orchestrator draws on, and the main team."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def build_pools(self) -> None:
        """The agent and skill pools from the current plugin set."""
        session = self._session
        settings = session.settings
        session.pool = AgentPool(
            db=session.db,
            broadcast=session.broadcast,
            prompt_loader=session.prompt_loader,
            supervisor=session.supervisor,
        )
        session.pool.load_definitions(
            settings,
            session.project_dir,
            group_dir=session.group_policy.shipped_dir("agents"),
        )
        session.plugin_loader.apply_to_agents(session.pool, disabled=session.disabled_plugins)
        AgentContributionApplier(
            session.prompt_contributions, prompt_loader=session.prompt_loader
        ).apply_to(session.pool)
        # One sub-agent cap for the session, drawn on by every
        # OrchestrateTools at every depth.
        session.spawn_budget = SpawnBudget(settings.orchestration.max_total_agents)
        if settings.orchestration.generate_ephemeral:
            session.pool.init_ephemeral(
                session.project_dir, settings.orchestration.max_ephemeral_per_session
            )
        session.pool.build_agents()
        # Checked once the pools are built: custom Python tools register
        # when discovered, and asking earlier would call them typos.
        session.agent_tool_check = self.check_tools()
        session.agent_tool_check.log_warnings()

        session.skill_pool = SkillPool()
        session.skill_pool.load_all(
            session.project_dir,
            settings.skills.cross_tool_support,
            group_dir=session.group_policy.shipped_dir("skills"),
        )
        session.plugin_loader.apply_to_skills(session.skill_pool, disabled=session.disabled_plugins)

    def check_tools(self) -> UnresolvedAgentTools:
        """Which of the agents' custom Python tools are not registered.

        A diagnostic, never a failure: an agent naming a tool that does
        not exist is worth a warning, not worth refusing to start.
        """
        session = self._session
        try:
            return UnresolvedAgentTools.check(
                session.pool.list_agents(),
                ToolRegistry(base_dir=str(session.project_dir), supervisor=session.supervisor),
            )
        except Exception as exc:  # noqa: BLE001 — a diagnostic must not stop a start
            logger.debug("Could not check agent tools: %s", exc)
            return UnresolvedAgentTools()

    def build_main_team(self) -> None:
        """The main team — the agent the session talks to."""
        session = self._session
        session.main_team = MainAgentBuilder(
            session,
            agent_cls=Agent,
            registry_cls=ToolRegistry,
            permissions_cls=ToolPermissions,
            compression_cls=MeasuredCompression,
            model_registry_cls=ModelRegistry,
            reasoning_factory=create_reasoning_tools,
            guardrails_factory=create_guardrails,
            prompt_loader=session.prompt_loader,
        ).build()

    def replace_tool_event_hook(self, hook: ToolEventHook) -> None:
        """Swap the tool-event hook on the live team.

        The team is built with the hook attached, but the hook is
        rebuilt whenever the hooks are reloaded, and the team is not.
        Agno owns the team's hook list and offers no setter, so the one
        hook that changed is swapped in place: the old tool-event hook
        is dropped and the new one appended.
        """
        team = self._session.main_team
        if team is None:
            return
        existing = team.tool_hooks or []
        team.tool_hooks = [h for h in existing if not isinstance(h, ToolEventHook)]
        team.tool_hooks.append(hook)


__all__ = ["AgentsBuilder"]
