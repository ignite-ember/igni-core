"""Rows of cells as a Markdown table, cut at a row cap."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pydantic import BaseModel

from igni_core.core.documents.budget import ReadBudget
from igni_core.core.documents.notes import DocumentNotes


class MarkdownTable(BaseModel):
    """The first ``cap`` non-empty rows of a table, the first as its header, and how many it had."""

    rows: list[list[str]]
    total: int
    cap: int

    @classmethod
    def collect(cls, rows: Iterable[Sequence[str]], cap: int, budget: ReadBudget) -> MarkdownTable:
        """The non-empty rows, counted to the end or until the time is up."""
        kept: list[list[str]] = []
        total = 0
        for row in rows:
            cells = [" ".join(c.split()).replace("|", "\\|") for c in row]
            if not any(cells):
                continue
            total += 1
            if total <= cap:
                kept.append(cells)
            if budget.overdue():
                break
        width = max((i + 1 for row in kept for i, c in enumerate(row) if c), default=0)
        return cls(rows=[(row + [""] * width)[:width] for row in kept], total=total, cap=cap)

    def render(self, budget: ReadBudget) -> str:
        """The table within ``budget``, and a note when rows were left out."""
        if not self.rows:
            return ""
        lines = ["| " + " | ".join(row) + " |" for row in self.rows]
        lines.insert(1, "|" + "---|" * len(self.rows[0]))
        text = budget.take("\n".join(lines))
        if self.total > self.cap and not budget.spent:
            text += "\n" + DocumentNotes.parts_cut("Rows", self.cap, self.total)
        return text
