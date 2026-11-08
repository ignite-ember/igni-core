"""Agents that name tools igni cannot resolve."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from igni_core.core.tools.tool_spec import ToolResolutionRequest

if TYPE_CHECKING:
    from igni_core.core.agents.schemas import AgentDefinition
    from igni_core.core.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class UnresolvedAgentTool(BaseModel):
    """One agent and the tool names it lists that will not resolve."""

    agent: str
    tools: list[str]


class UnresolvedAgentTools(BaseModel):
    """The startup check of every agent's tool list.

    Not fatal: such an agent loads and simply raises the moment anything
    calls it, so this is how somebody hears about it at startup instead.
    """

    entries: list[UnresolvedAgentTool] = Field(default_factory=list)
    available: list[str] = Field(default_factory=list)

    @classmethod
    def check(
        cls, definitions: Iterable[AgentDefinition], registry: ToolRegistry
    ) -> UnresolvedAgentTools:
        """Agents among ``definitions`` naming tools ``registry`` cannot resolve.

        Trustworthy only once the pools are built: custom Python tools
        register when they are discovered.
        """
        entries = []
        for defn in definitions:
            if not defn.tools:
                continue
            result = registry.resolve_typed(ToolResolutionRequest(tool_names=list(defn.tools)))
            if result.unknown:
                entries.append(UnresolvedAgentTool(agent=defn.name, tools=list(result.unknown)))
        return cls(entries=entries, available=list(registry.available_tools))

    def by_agent(self) -> dict[str, list[str]]:
        return {entry.agent: list(entry.tools) for entry in self.entries}

    def log_warnings(self) -> None:
        for entry in self.entries:
            logger.warning(
                "Agent %r names %s, which igni cannot resolve — it will fail when "
                "something calls it. Available: %s",
                entry.agent,
                ", ".join(repr(n) for n in entry.tools),
                ", ".join(self.available),
            )


__all__ = ["UnresolvedAgentTool", "UnresolvedAgentTools"]
