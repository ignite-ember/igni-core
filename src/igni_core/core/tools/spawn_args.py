"""Typed views of the calls a spawned agent's run hangs from.

:class:`~igni_core.core.tools.spawn_context.ParentCall` stores a call's
arguments and its run's messages as Agno serialises them; these models
are how the rest of the code reads them.

* :class:`SpawnArgs` — the arguments of a ``spawn_agent`` /
  ``spawn_team`` call.
* :class:`TurnToolCall` — one tool call of an assistant turn, from the
  wire shape Agno keeps in a message's ``tool_calls``.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SpawnArgs(BaseModel):
    """What a ``spawn_agent`` or ``spawn_team`` call was given."""

    model_config = ConfigDict(extra="ignore")

    task: str = ""
    #: ``spawn_agent``'s agent.
    agent_name: str = ""
    #: ``spawn_team``'s agents, comma-separated.
    agent_names: str = ""
    #: ``spawn_team``'s mode; empty when the call gave none.
    mode: str = ""
    isolation: str = ""

    @field_validator("task", "agent_name", "agent_names", "mode", "isolation", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        return "" if value is None else str(value)

    @property
    def team_names(self) -> list[str]:
        return [n.strip() for n in self.agent_names.split(",") if n.strip()]

    @property
    def agent_label(self) -> str:
        """Who the call ran, as a single-agent spawn names it."""
        return self.agent_name or self.agent_names


class TurnToolCall(BaseModel):
    """One tool call of an assistant turn."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_wire(cls, call: dict[str, Any]) -> TurnToolCall:
        """From Agno's ``{"id": ..., "function": {"name": ..., "arguments": ...}}``."""
        function = call.get("function") or {}
        return cls(
            id=str(call.get("id") or ""),
            name=str(function.get("name") or ""),
            arguments=cls.parse_arguments(function.get("arguments")),
        )

    @staticmethod
    def parse_arguments(raw: Any) -> dict[str, Any]:
        """A call's arguments: a JSON object string, or already a dict.
        Anything else reads as no arguments."""
        if isinstance(raw, dict):
            return dict(raw)
        try:
            parsed = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}


__all__ = ["SpawnArgs", "TurnToolCall"]
