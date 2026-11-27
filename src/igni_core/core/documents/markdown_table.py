"""Rows of cells as a Markdown table, cut at a row cap."""

from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence

from pydantic import BaseModel, Field

from igni_core.core.documents.budget import ReadBudget
from igni_core.core.documents.notes import DocumentNotes
from igni_core.core.documents.window import ReadWindow


class MarkdownTable(BaseModel):
    """Up to ``cap`` non-empty rows of a table from its window, under its header, and how many it had."""

    rows: list[list[str]]
    total: int
    cap: int
    numbers: list[int] = Field(default_factory=list)
    """Each kept row's number among the non-empty rows; the header is row 1."""
    window: ReadWindow = Field(default_factory=ReadWindow)
    stopped_at: int = 0
    """The row the character or time cap stopped in; 0 when it did not."""

    @classmethod
    def collect(
        cls,
        rows: Iterable[Sequence[str]],
        cap: int,
        budget: ReadBudget,
        window: ReadWindow | None = None,
    ) -> MarkdownTable:
        """The non-empty rows in ``window``, counted to the end or until the time is up."""
        window = window or ReadWindow()
        last = window.end(cap, sys.maxsize)
        kept: list[list[str]] = []
        numbers: list[int] = []
        total = 0
        for row in rows:
            cells = [" ".join(c.split()).replace("|", "\\|") for c in row]
            if not any(cells):
                continue
            total += 1
            if window.first <= total <= last or total == 1:
                kept.append(cells)
                numbers.append(total)
            if budget.overdue():
                break
        width = max((i + 1 for row in kept for i, c in enumerate(row) if c), default=0)
        return cls(
            rows=[(row + [""] * width)[:width] for row in kept],
            total=total,
            cap=cap,
            numbers=numbers,
            window=window,
        )

    def render(self, budget: ReadBudget) -> str:
        """The table within ``budget``, and a note when rows were left out."""
        if not self.rows:
            return ""
        lines = ["| " + " | ".join(row) + " |" for row in self.rows]
        lines.insert(1, "|" + "---|" * len(self.rows[0]))
        table = "\n".join(lines)
        text = budget.take(table)
        if len(text) < len(table):
            self.stopped_at = self._row_at(text)
        if self.window.capped(self.cap, self.total) and not budget.spent:
            shown = self.window.end(self.cap, self.total)
            note = DocumentNotes.parts_cut("Rows", shown, self.total)
            text += "\n" + self.window.note(note, self.window.after(shown))
        return text

    def _row_at(self, text: str) -> int:
        """The row whose line ``text`` stops in, counting the separator line as the next row's."""
        line = text.count("\n")
        index = 0 if line == 0 else min(max(line - 1, 1), len(self.numbers) - 1)
        return max(self.numbers[index], self.window.first)
