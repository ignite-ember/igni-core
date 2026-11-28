"""One line of a user's memory, as the user-memory store keeps it."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class StoredMemory(BaseModel):
    """A stored memory's id and text."""

    id: str
    content: str

    @classmethod
    def all_of(cls, data: Any) -> list[StoredMemory]:
        """Every memory in Agno's ``Memories`` record, in stored order; none for no record."""
        entries = getattr(data, "memories", None) or []
        return [
            cls(id=str(entry.get("id", "")), content=str(entry.get("content", "")))
            for entry in entries
            if isinstance(entry, dict)
        ]
