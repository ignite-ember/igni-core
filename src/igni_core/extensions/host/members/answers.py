"""What members answer when it is more than one backend value.

Each is named after the host schema's type and read from what igni's RPC
handlers return, in one place, so the member that answers with it does
not build the shape by hand.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ContextUsage(BaseModel):
    """``chat.context``: the context's size, its limit, and what fills it."""

    tokens: int
    max: int
    #: ``get_context_breakdown``, as the backend gives it.
    breakdown: Any


class CompactResult(BaseModel):
    """``chat.compact``: the session the history moved to, and its summary."""

    session_id: str
    new_session_id: str
    summary: str

    @classmethod
    def from_rpc(cls, session_id: str, cleared: Any) -> CompactResult | None:
        """``compact_if_needed``'s answer; nothing when nothing was compacted."""
        if not cleared:
            return None
        return cls(
            session_id=session_id,
            new_session_id=str(cleared.get("new_session_id", "")),
            summary=str(cleared.get("summary", "")),
        )


class PlanSnapshot(BaseModel):
    """``plan.latest``: the newest plan, its history, tasks and state."""

    latest: str
    history: list[Any]
    tasks: list[Any]
    state: str

    @classmethod
    def from_rpc(cls, found: dict[str, Any]) -> PlanSnapshot:
        return cls(
            latest=str(found.get("latest", "")),
            history=list(found.get("history") or []),
            tasks=list(found.get("tasks") or []),
            state=str(found.get("state", "")),
        )


__all__ = ["CompactResult", "ContextUsage", "PlanSnapshot"]
