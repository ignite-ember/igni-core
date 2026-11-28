"""One broken invariant, at one step of a scenario."""

from __future__ import annotations

from pydantic import BaseModel


class Violation(BaseModel):
    """Which invariant broke, after which step, and what was seen."""

    invariant: str
    step: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.invariant}] after {self.step}: {self.detail}"
