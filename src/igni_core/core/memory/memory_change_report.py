"""What an ``update_user_memory`` call changed, read from the store before and after it."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from igni_core.core.memory.stored_memory import StoredMemory


class MemoryChangeReport(BaseModel):
    """The memories before and after one update, and the reply that says what changed."""

    #: Agno's own reply when the extraction changed nothing.
    UNCHANGED: ClassVar[str] = "No updates needed"

    before: list[StoredMemory] = Field(default_factory=list)
    after: list[StoredMemory] = Field(default_factory=list)

    def reply(self) -> str:
        """One line per memory saved, updated or deleted, each with its stored text."""
        was = {m.id: m.content for m in self.before}
        now = {m.id: m.content for m in self.after}
        lines = [
            f"Memory saved: {m.content}" if m.id not in was else f"Memory updated: {m.content}"
            for m in self.after
            if was.get(m.id) != m.content
        ]
        lines += [f"Memory deleted: {m.content}" for m in self.before if m.id not in now]
        return "\n".join(lines) if lines else self.UNCHANGED
