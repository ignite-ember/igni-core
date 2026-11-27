"""An OpenDocument spreadsheet, each sheet as a Markdown table under its name."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar

from lxml import etree

from igni_core.core.documents.odf_package import OdfPackage
from igni_core.core.documents.odf_text import OdfText
from igni_core.core.documents.workbook_reader import WorkbookDocumentReader
from igni_core.core.documents.workbook_sheet import WorkbookSheet


class OdsDocumentReader(WorkbookDocumentReader):
    """Cells as last shown: formulas never evaluated, macros never run, repeats expanded only up to a sheet's real size."""

    extensions: ClassVar[frozenset[str]] = frozenset({".ods"})
    MAX_SHEET_COLUMNS: ClassVar[int] = 16_384
    """Excel's own limit: a repeat count past it is a hostile file, not a sheet."""
    MAX_SHEET_CELLS: ClassVar[int] = 2_000_000
    """Repeated rows stop being copied here, so a row claiming a billion copies costs a second, not the time cap."""

    @contextmanager
    def _workbook(self, path: Path) -> Iterator[Sequence[WorkbookSheet]]:
        body = OdfPackage.body(path, self.MAX_UNZIPPED_BYTES, self.MAX_PART_BYTES, "spreadsheet")
        yield [self._sheet(table) for table in body.findall(OdfText.tag(OdfText.TABLE, "table"))]

    def _sheet(self, table: etree._Element) -> WorkbookSheet:
        title = table.get(OdfText.tag(OdfText.TABLE, "name")) or ""
        return WorkbookSheet(title, lambda: self._rows(table))

    def _rows(self, table: etree._Element) -> Iterator[list[str]]:
        repeated = OdfText.tag(OdfText.TABLE, "number-rows-repeated")
        left = self.MAX_SHEET_CELLS
        for row in OdfText.rows(table):
            cells = self._cells(row)
            copies = 0
            if any(cells):
                copies = min(self._count(row.get(repeated)) - 1, left // len(cells))
                left -= copies * len(cells)
            for _ in range(1 + copies):
                yield cells

    def _cells(self, row: etree._Element) -> list[str]:
        repeated = OdfText.tag(OdfText.TABLE, "number-columns-repeated")
        cells: list[str] = []
        for cell in OdfText.cells(row):
            room = self.MAX_SHEET_COLUMNS - len(cells)
            cells.extend([OdfText.cell(cell)] * min(self._count(cell.get(repeated)), room))
        while cells and not cells[-1]:
            cells.pop()
        return cells

    @staticmethod
    def _count(value: str | None) -> int:
        """A repeat attribute's count; 1 when it is missing or not a number."""
        return int(value) if value and value.isdigit() and int(value) > 0 else 1
