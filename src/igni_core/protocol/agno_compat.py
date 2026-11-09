"""Agno compatibility: the two shapes a run comes in.

Agno names the producer of a run one of two ways: an agent's run
carries ``agent_id`` / ``agent_name``, a team's run carries
``team_id`` / ``team_name``. One stream carries both — a team's own
events and its members' events arrive interleaved — so a consumer that
wants the producer has to know which spelling the event uses.

:class:`RunProducer` is that knowledge in one place: it reads the
producer off an Agno event or run output and hands back a typed value.

Agno's classes are never patched to answer to the other spelling. A
team that answers to ``agent_id`` cannot be told apart from an agent
downstream, and a patch applied at import time is a side effect on a
third-party class that no call site asked for. The producer is read
where a run is consumed — the only place that knows which shape it is
holding.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


def _text(value: Any) -> str:
    """The value as a string; empty when it is missing or falsy."""
    return str(value) if value else ""


class RunProducer(BaseModel):
    """Who produced a run: the agent that ran, or the team it ran under.

    ``kind`` says which of Agno's two shapes the producer was read from,
    so a caller that has to tell an agent's run from a team's can. It
    is ``"unknown"`` for a source that names no producer at all.
    """

    kind: Literal["agent", "team", "unknown"] = "unknown"
    name: str = ""

    @classmethod
    def from_agno(cls, source: Any) -> RunProducer:
        """Read the producer off an Agno event or run output.

        Agno gives an agent's run ``agent_name`` and a team's run
        ``team_name``. The agent spelling wins when a source carries
        both — a run has one producer.
        """
        agent_name = _text(getattr(source, "agent_name", None))
        if agent_name:
            return cls(kind="agent", name=agent_name)
        team_name = _text(getattr(source, "team_name", None))
        if team_name:
            return cls(kind="team", name=team_name)
        return cls()


__all__ = ["RunProducer"]
