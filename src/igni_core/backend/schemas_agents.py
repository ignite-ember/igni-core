"""Typed view models for the ``/agents`` slash command's chat output.

Every markdown template the :class:`AgentsCommand` coordinator emits
lives here as a Pydantic view model with a single
``.to_command_result()`` render entry point. Mirrors the sibling
:mod:`schemas_context` pattern.
"""

from __future__ import annotations

from pydantic import BaseModel

from igni_core.backend.command_result import CommandResult
from igni_core.core.agents.schemas import AgentDefinition


class PromoteResult(BaseModel):
    """Outcome of :meth:`AgentPool.promote_ephemeral` wrapped as a
    typed result envelope. The pool itself still raises (tests
    lock that in) — the coordinator catches once and packages it.
    """

    ok: bool
    destination: str | None = None
    error: str | None = None

    def to_command_result(self, name: str) -> CommandResult:
        if self.ok:
            return CommandResult.info(f"Promoted '{name}' to {self.destination}")
        return CommandResult.error(self.error or "unknown error")


class DiscardResult(BaseModel):
    """Outcome of :meth:`AgentPool.discard_ephemeral` — same shape
    as :class:`PromoteResult` minus the destination field.
    """

    ok: bool
    error: str | None = None

    def to_command_result(self, name: str) -> CommandResult:
        if self.ok:
            return CommandResult.info(f"Discarded ephemeral agent '{name}'.")
        return CommandResult.error(self.error or "unknown error")


class EphemeralAgentsView(BaseModel):
    """Wraps the ``list[AgentDefinition]`` returned by
    :meth:`AgentPool.list_ephemeral`. Empty list renders as an
    info result; non-empty renders a markdown catalogue.

    ``AgentDefinition`` is imported at runtime, not under
    ``TYPE_CHECKING``: Pydantic resolves field annotations against the
    module namespace, so a type-only import leaves the model
    unconstructible.
    """

    agents: list[AgentDefinition]

    def to_command_result(self) -> CommandResult:
        if not self.agents:
            return CommandResult.info("No ephemeral agents.")
        rows = "".join(self._row(defn) for defn in self.agents)
        footer = "\n*`/agents promote <name>` to save · `/agents discard <name>` to remove*\n"
        return CommandResult.markdown(f"## Ephemeral Agents\n{rows}{footer}")

    @staticmethod
    def _row(defn: AgentDefinition) -> str:
        tools = ", ".join(defn.tools) if defn.tools else "none"
        return f"- **{defn.name}** — {defn.description}\n  tools: {tools}\n"


__all__ = ["PromoteResult", "DiscardResult", "EphemeralAgentsView"]
