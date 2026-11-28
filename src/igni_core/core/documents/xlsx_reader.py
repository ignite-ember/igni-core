"""An Excel workbook, each sheet as a Markdown table under its name."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ClassVar

from openpyxl import load_workbook

from igni_core.core.documents.ooxml_archive import OoxmlArchive
from igni_core.core.documents.workbook_reader import WorkbookDocumentReader
from igni_core.core.documents.workbook_sheet import WorkbookSheet


class XlsxDocumentReader(WorkbookDocumentReader):
    """Cell values as last saved: formulas are never evaluated, links never followed."""

    extensions: ClassVar[frozenset[str]] = frozenset({".xlsx", ".xlsm"})

    @contextmanager
    def _workbook(self, path: Path) -> Iterator[Sequence[WorkbookSheet]]:
        OoxmlArchive.check(path, self.MAX_UNZIPPED_BYTES)
        workbook = load_workbook(
            path, read_only=True, data_only=True, keep_links=False, keep_vba=False
        )
        try:
            yield [self._sheet(sheet) for sheet in workbook.worksheets]
        finally:
            workbook.close()

    def _sheet(self, sheet: Any) -> WorkbookSheet:
        return WorkbookSheet(sheet.title, lambda: self.row_texts(sheet.iter_rows(values_only=True)))
