"""One known defect the matrix exposes."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Finding(BaseModel):
    """A finding's id, what it is, and the invariants its cells break."""

    finding_id: str
    title: str
    invariants: frozenset[str] = Field(default_factory=frozenset)
