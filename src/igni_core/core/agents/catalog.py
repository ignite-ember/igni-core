"""The specialist-agent catalog the main agent's system prompt lists."""

from __future__ import annotations

from collections.abc import Iterable

from igni_core.core.agents.schemas import AgentDefinition


def agent_catalog(definitions: Iterable[AgentDefinition]) -> str:
    """One markdown bullet per agent: its name, description and tools."""
    lines = []
    for defn in definitions:
        tools_str = ", ".join(defn.tools) if defn.tools else "none"
        lines.append(f"- **{defn.name}**: {defn.description} (tools: {tools_str})")
    return "\n".join(lines)


__all__ = ["agent_catalog"]
