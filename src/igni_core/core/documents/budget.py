"""The characters and seconds one document may take."""

from __future__ import annotations

import time

from pydantic import BaseModel

from igni_core.core.documents.notes import DocumentNotes


class ReadBudget(BaseModel):
    """How much text a reader may still give, and until when."""

    chars: int
    seconds: int
    deadline: float
    used: int = 0
    cut: bool = False
    late: bool = False

    @classmethod
    def start(cls, chars: int, seconds: int) -> ReadBudget:
        """A budget whose clock starts now."""
        return cls(chars=chars, seconds=seconds, deadline=time.monotonic() + seconds)

    def overdue(self) -> bool:
        """Whether the time is up; once it is, it stays up."""
        if not self.late and time.monotonic() > self.deadline:
            self.late = True
        return self.late

    def take(self, text: str) -> str:
        """As much of ``text`` as the budget still allows."""
        if self.overdue() or self.cut:
            return ""
        room = self.chars - self.used
        if len(text) > room:
            self.cut = True
            self.used = self.chars
            return text[:room]
        self.used += len(text)
        return text

    @property
    def spent(self) -> bool:
        """Whether the reader should stop."""
        return self.cut or self.late

    def notes(self) -> list[str]:
        """The note for whichever cap stopped the text."""
        if self.cut:
            return [DocumentNotes.chars_cut(self.chars)]
        if self.late:
            return [DocumentNotes.time_cut(self.seconds)]
        return []
