"""An Excel workbook, each sheet as a Markdown table under its name."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import ClassVar

from openpyxl import load_workbook

from igni_core.core.documents.budget import ReadBudget
from igni_core.core.documents.markdown_table import MarkdownTable
from igni_core.core.documents.notes import DocumentNotes
from igni_core.core.documents.ooxml_archive import OoxmlArchive
from igni_core.core.documents.reader import DocumentReader
from igni_core.core.documents.schemas import DocumentText


class XlsxDocumentReader(DocumentReader):
    """Cell values as last saved: formulas are never evaluated, links never followed."""

    extensions: ClassVar[frozenset[str]] = frozenset({".xlsx", ".xlsm"})

    def _read(self, path: Path, budget: ReadBudget) -> DocumentText:
        OoxmlArchive.check(path, self.MAX_UNZIPPED_BYTES)
        workbook = load_workbook(
            path, read_only=True, data_only=True, keep_links=False, keep_vba=False
        )
        try:
            sheets = workbook.worksheets
            parts: list[str] = []
            has_text = False
            for sheet in sheets[: self.MAX_SHEETS]:
                values = ([self._cell(v) for v in row] for row in sheet.iter_rows(values_only=True))
                table = MarkdownTable.collect(values, self.MAX_ROWS, budget)
                has_text = has_text or bool(table.rows)
                rendered = table.render(budget)
                part = f"--- Sheet: {sheet.title} ---" + (f"\n{rendered}" if rendered else "")
                parts.append(part)
                if budget.spent:
                    break
        finally:
            workbook.close()
        notes = budget.notes()
        if len(sheets) > self.MAX_SHEETS and not budget.spent:
            notes.append(DocumentNotes.parts_cut("Sheets", self.MAX_SHEETS, len(sheets)))
        return DocumentText(
            path=str(path),
            count=DocumentNotes.counted(len(sheets), "sheet"),
            body="\n".join(parts),
            has_text=has_text,
            notes=notes,
        )

    @staticmethod
    def _cell(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        if isinstance(value, datetime.datetime) and value.time() == datetime.time():
            return value.date().isoformat()
        if isinstance(value, datetime.date | datetime.time):
            return value.isoformat()
        return str(value)
