"""Which part of a document to read, and the part a cut tells the model to read next."""

from __future__ import annotations

import re
from typing import ClassVar

from pydantic import BaseModel

from igni_core.core.documents.budget import ReadBudget
from igni_core.core.documents.exceptions import DocumentRefused
from igni_core.core.documents.notes import DocumentNotes


class ReadWindow(BaseModel):
    """Units ``first`` to ``last`` of a document, or rows of one ``sheet``; whole from the start by default."""

    first: int = 1
    last: int | None = None
    sheet: str | None = None
    part: str = ""
    """The ``part`` argument as the model wrote it."""
    hinted: bool = False
    """Whether a cut says which part to read next: only for the ``read_document`` tool."""

    _RANGE: ClassVar[re.Pattern[str]] = re.compile(r"(\d+)\s*(?:(-)\s*(\d*))?")

    @classmethod
    def parse(cls, part: str, path: str) -> ReadWindow:
        """The window ``part`` names: ``"21-40"``, ``"21-"``, ``"21"`` or ``"Sheet!501-"``."""
        text = part.strip()
        sheet: str | None = None
        if "!" in text:
            sheet, _, text = text.rpartition("!")
            if not sheet.strip():
                raise DocumentRefused(DocumentNotes.bad_part(path, part))
            text = text.strip() or "1-"
        if not text:
            return cls(part=part, hinted=True)
        matched = cls._RANGE.fullmatch(text)
        if matched is None:
            raise DocumentRefused(DocumentNotes.bad_part(path, part))
        first = int(matched.group(1))
        last = int(matched.group(3)) if matched.group(3) else None
        if not matched.group(2):
            last = first
        if first < 1 or (last is not None and last < first):
            raise DocumentRefused(DocumentNotes.bad_part(path, part))
        return cls(first=first, last=last, sheet=sheet, part=part, hinted=True)

    def rows_of(self, sheet: str) -> ReadWindow:
        """All rows of ``sheet``, from the first, hinted as this window is."""
        return ReadWindow(sheet=sheet, hinted=self.hinted)

    def end(self, cap: int, total: int) -> int:
        """The last unit shown: the asked-for last, the cap or the document's end, whichever comes first."""
        end = self.first + cap - 1
        if self.last is not None:
            end = min(end, self.last)
        return min(end, total)

    def capped(self, cap: int, total: int) -> bool:
        """Whether the cap, not the asked-for last unit, stopped short of the document's end."""
        limit = self.first + cap - 1
        return total > limit and (self.last is None or self.last > limit)

    def past(self, total: int) -> bool:
        """Whether the window starts after the document's last unit."""
        return self.first > 1 and self.first > total

    def past_end(self, path: str, count: str) -> DocumentRefused:
        """The refusal for a window that starts after the document's end."""
        return DocumentRefused(DocumentNotes.past_end(path, self.part, count))

    def after(self, shown: int) -> str | None:
        """The part that follows unit ``shown``; ``None`` past the asked-for end or when not hinted."""
        start = shown + 1
        if not self.hinted or (self.last is not None and start > self.last):
            return None
        span = f"{start}-{self.last}" if self.last is not None else f"{start}-"
        return f"{self.sheet}!{span}" if self.sheet is not None else span

    def resume(self, cut: int) -> str | None:
        """The part that starts again at unit ``cut``, shown only in part; past it when it was the first."""
        return self.after(cut - 1 if cut > self.first else cut)

    def continuation(self, budget: ReadBudget, stopped: int) -> str | None:
        """The part to read next when ``budget`` stopped the read at unit ``stopped``."""
        if budget.cut:
            return self.resume(stopped)
        if budget.late:
            return self.after(stopped - 1)
        return None

    @staticmethod
    def note(note: str, part: str | None) -> str:
        """``note``, ending with how to read ``part`` when there is one."""
        return DocumentNotes.read_on(note, part) if part else note
