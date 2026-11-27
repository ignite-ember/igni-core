"""A workbook, each sheet as a Markdown table under its name: the shape Excel and OpenDocument share."""

from __future__ import annotations

import datetime
from abc import abstractmethod
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import ClassVar

from igni_core.core.documents.budget import ReadBudget
from igni_core.core.documents.exceptions import DocumentRefused
from igni_core.core.documents.markdown_table import MarkdownTable
from igni_core.core.documents.notes import DocumentNotes
from igni_core.core.documents.reader import DocumentReader
from igni_core.core.documents.schemas import DocumentText
from igni_core.core.documents.window import ReadWindow
from igni_core.core.documents.workbook_sheet import WorkbookSheet


class WorkbookDocumentReader(DocumentReader):
    """Sheets under ``--- Sheet: name ---``; a part counts sheets, or rows of the sheet it names."""

    NAMED_SHEETS: ClassVar[bool] = True

    @abstractmethod
    def _workbook(self, path: Path) -> AbstractContextManager[Sequence[WorkbookSheet]]:
        """The workbook's sheets, open while the context lasts; may raise."""

    def _read(self, path: Path, budget: ReadBudget, window: ReadWindow) -> DocumentText:
        with self._workbook(path) as sheets:
            if window.sheet is not None:
                return self._one_sheet(str(path), sheets, budget, window)
            return self._sheets(str(path), sheets, budget, window)

    def _sheets(
        self, path: str, sheets: Sequence[WorkbookSheet], budget: ReadBudget, window: ReadWindow
    ) -> DocumentText:
        count = DocumentNotes.counted(len(sheets), "sheet")
        if window.past(len(sheets)):
            raise window.past_end(path, count)
        end = window.end(self.MAX_SHEETS, len(sheets))
        parts: list[str] = []
        has_text = False
        stopped = 0
        table: MarkdownTable | None = None
        for n in range(window.first, end + 1):
            sheet = sheets[n - 1]
            table = self._table(sheet, budget, window.rows_of(sheet.title))
            has_text = has_text or bool(table.rows)
            parts.append(self._part(sheet.title, table.render(budget)))
            if budget.spent:
                stopped = n
                break
        notes = self._cut_notes(budget, window, stopped, table, len(sheets))
        if window.capped(self.MAX_SHEETS, len(sheets)) and not budget.spent:
            cut = DocumentNotes.parts_cut("Sheets", end, len(sheets))
            notes.append(window.note(cut, window.after(end)))
        return DocumentText(
            path=path, count=count, body="\n".join(parts), has_text=has_text, notes=notes
        )

    def _one_sheet(
        self, path: str, sheets: Sequence[WorkbookSheet], budget: ReadBudget, window: ReadWindow
    ) -> DocumentText:
        sheet = self._find(sheets, window.sheet or "")
        if sheet is None:
            raise DocumentRefused(DocumentNotes.no_sheet(path, window.sheet or ""))
        table = self._table(sheet, budget, window.model_copy(update={"sheet": sheet.title}))
        if table.window.past(table.total) and not budget.late:
            raise table.window.past_end(path, DocumentNotes.counted(table.total, "row"))
        body = self._part(sheet.title, table.render(budget))
        stopped = table.stopped_at or table.window.first
        return DocumentText(
            path=path,
            count=DocumentNotes.counted(len(sheets), "sheet"),
            body=body,
            has_text=bool(table.rows),
            notes=budget.notes(table.window.continuation(budget, stopped)),
        )

    @staticmethod
    def _cut_notes(
        budget: ReadBudget,
        window: ReadWindow,
        stopped: int,
        table: MarkdownTable | None,
        total: int,
    ) -> list[str]:
        """The cap's note: read on from the stopped sheet, or within it and then past it when it was the first."""
        if not budget.spent:
            return []
        if stopped > window.first or table is None:
            return budget.notes(window.continuation(budget, stopped))
        notes = budget.notes(table.window.continuation(budget, table.stopped_at or 1))
        following = window.after(stopped)
        if following and stopped < total:
            notes.append(window.note(DocumentNotes.parts_cut("Sheets", stopped, total), following))
        return notes

    def _table(self, sheet: WorkbookSheet, budget: ReadBudget, window: ReadWindow) -> MarkdownTable:
        return MarkdownTable.collect(sheet.rows(), self.MAX_ROWS, budget, window)

    @staticmethod
    def _part(title: str, rendered: str) -> str:
        return f"--- Sheet: {title} ---" + (f"\n{rendered}" if rendered else "")

    @staticmethod
    def _find(sheets: Sequence[WorkbookSheet], name: str) -> WorkbookSheet | None:
        """The sheet called ``name``, or failing that the one sheet whose name differs only in case."""
        exact = [sheet for sheet in sheets if sheet.title == name]
        if exact:
            return exact[0]
        loose = [sheet for sheet in sheets if sheet.title.strip().lower() == name.strip().lower()]
        return loose[0] if len(loose) == 1 else None

    @staticmethod
    def cell_text(value: object) -> str:
        """A cell's saved value as text: a whole float without ``.0``, a midnight datetime as its date."""
        if value is None:
            return ""
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        if isinstance(value, datetime.datetime) and value.time() == datetime.time():
            return value.date().isoformat()
        if isinstance(value, datetime.date | datetime.time):
            return value.isoformat()
        return str(value)

    @classmethod
    def row_texts(cls, rows: Iterator[Sequence[object]]) -> Iterator[list[str]]:
        """Each row of saved values as cell texts."""
        for row in rows:
            yield [cls.cell_text(value) for value in row]
