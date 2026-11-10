"""Agents panel controller.

Owns the agents-panel concern: snapshot the pool for the panel
UI, plus the promote/discard mutators for ephemeral agents.
Uses :meth:`EphemeralAgentStore.owns` to tag ephemerals and returns typed
:class:`PromoteEphemeralResult` / :class:`DiscardEphemeralResult`
objects rather than overloaded ``msg.Info`` toasts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from igni_core.backend.schemas_panels import (
    DiscardEphemeralResult,
    PromoteEphemeralResult,
)
from igni_core.core.agents import AgentInfo

if TYPE_CHECKING:
    from igni_core.core.session import Session


class AgentsPanelController:
    """Snapshot + mutators for the agents panel."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def snapshot(self) -> list[AgentInfo]:
        """Snapshot of every loaded agent for the panel UI."""
        pool = self._session.pool
        unknown = self._session.agent_tool_check.by_agent()
        results: list[AgentInfo] = []
        store = pool.ephemeral if pool.has_ephemeral else None
        for defn in pool.list_agents():
            results.append(
                AgentInfo(
                    name=defn.name,
                    description=defn.description,
                    tools=list(defn.tools),
                    model=defn.model or "",
                    color=defn.color or "",
                    can_orchestrate=defn.can_orchestrate,
                    mcp_servers=list(defn.mcp_servers),
                    tags=list(defn.tags),
                    system_prompt=defn.system_prompt,
                    source_path=str(defn.source_path) if defn.source_path else "",
                    is_ephemeral=store is not None and store.owns(defn),
                    unknown_tools=unknown.get(defn.name, []),
                )
            )
        return results

    def promote(self, name: str, target: str = "project") -> PromoteEphemeralResult:
        """Save an ephemeral agent permanently (called from the panel).

        ``target``: ``"project"`` (``.igni/agents/``, shared through git) or
        ``"personal"`` (``.igni/agents.local/``, yours only).
        """
        try:
            dest = self._session.pool.ephemeral.promote(name, self._session.project_dir, target)
        except (ValueError, RuntimeError) as e:
            return PromoteEphemeralResult(ok=False, name=name, reason=str(e))
        return PromoteEphemeralResult(ok=True, name=name, dest=str(dest))

    def discard(self, name: str) -> DiscardEphemeralResult:
        """Delete an ephemeral agent (called from the panel)."""
        try:
            self._session.pool.ephemeral.discard(name)
        except (ValueError, RuntimeError) as e:
            return DiscardEphemeralResult(ok=False, name=name, reason=str(e))
        return DiscardEphemeralResult(ok=True, name=name)
