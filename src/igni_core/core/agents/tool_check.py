"""Agents that name tools igni cannot resolve."""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

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
