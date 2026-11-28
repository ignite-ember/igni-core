"""A legacy Excel (.xls) workbook, each sheet as a Markdown table under its name."""

from __future__ import annotations

import io
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ClassVar

import xlrd
from xlrd.biffh import XLRDError

from igni_core.core.documents.exceptions import DocumentRefused
from igni_core.core.documents.notes import DocumentNotes
from igni_core.core.documents.workbook_reader import WorkbookDocumentReader
from igni_core.core.documents.workbook_sheet import WorkbookSheet


class XlsDocumentReader(WorkbookDocumentReader):
    """Cell values as last saved, through xlrd: formulas never evaluated, macros never run."""

    extensions: ClassVar[frozenset[str]] = frozenset({".xls"})

    @contextmanager
    def _workbook(self, path: Path) -> Iterator[Sequence[WorkbookSheet]]:
        try:
            book = xlrd.open_workbook(
                str(path), logfile=io.StringIO(), on_demand=True, ragged_rows=True
            )
        except XLRDError as error:
            if "encrypted" in str(error).lower():
                raise DocumentRefused(DocumentNotes.password_protected(str(path))) from error
            raise
        try:
            yield [self._sheet(book, index, name) for index, name in enumerate(book.sheet_names())]
        finally:
            book.release_resources()

    def _sheet(self, book: Any, index: int, name: str) -> WorkbookSheet:
        return WorkbookSheet(name, lambda: self._rows(book, index))

    def _rows(self, book: Any, index: int) -> Iterator[list[str]]:
        sheet = book.sheet_by_index(index)
        for r in range(sheet.nrows):
            yield [self._cell(book, cell) for cell in sheet.row(r)]

    def _cell(self, book: Any, cell: Any) -> str:
        if cell.ctype == xlrd.XL_CELL_DATE:
            try:
                return self.cell_text(xlrd.xldate.xldate_as_datetime(cell.value, book.datemode))
            except (ValueError, OverflowError, xlrd.xldate.XLDateError):
                return self.cell_text(cell.value)
        if cell.ctype == xlrd.XL_CELL_BOOLEAN:
            return str(bool(cell.value))
        if cell.ctype == xlrd.XL_CELL_ERROR:
            return xlrd.error_text_from_code.get(cell.value, "")
        return self.cell_text(cell.value if cell.ctype != xlrd.XL_CELL_EMPTY else None)
