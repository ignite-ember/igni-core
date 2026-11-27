"""A CSV or TSV file as a Markdown table."""

from __future__ import annotations

import codecs
import csv
import io
from pathlib import Path
from typing import ClassVar

from igni_core.core.documents.budget import ReadBudget
from igni_core.core.documents.markdown_table import MarkdownTable
from igni_core.core.documents.notes import DocumentNotes
from igni_core.core.documents.reader import DocumentReader
from igni_core.core.documents.schemas import DocumentText
from igni_core.core.documents.window import ReadWindow


class CsvDocumentReader(DocumentReader):
    """Sniffs the delimiter and the encoding; the first row is the header."""

    extensions: ClassVar[frozenset[str]] = frozenset({".csv", ".tsv"})
    _DELIMITERS: ClassVar[str] = ",;\t|"
    _SNIFF_CHARS: ClassVar[int] = 64 * 1024

    def _read(self, path: Path, budget: ReadBudget, window: ReadWindow) -> DocumentText:
        text = self._decode(path.read_bytes())
        delimiter = self._delimiter(path, text)
        rows = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        table = MarkdownTable.collect(rows, self.MAX_ROWS, budget, window)
        count = DocumentNotes.counted(table.total, "row")
        if window.past(table.total) and not budget.late:
            raise window.past_end(str(path), count)
        body = table.render(budget)
        return DocumentText(
            path=str(path),
            count=count,
            body=body,
            has_text=bool(table.rows),
            notes=budget.notes(window.continuation(budget, table.stopped_at or window.first)),
        )

    @staticmethod
    def _decode(raw: bytes) -> str:
        if raw.startswith(codecs.BOM_UTF8):
            return raw[len(codecs.BOM_UTF8) :].decode("utf-8", errors="replace")
        if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            return raw.decode("utf-16", errors="replace")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("cp1252", errors="replace")

    def _delimiter(self, path: Path, text: str) -> str:
        if path.suffix.lower() == ".tsv":
            return "\t"
        try:
            return csv.Sniffer().sniff(text[: self._SNIFF_CHARS], self._DELIMITERS).delimiter
        except csv.Error:
            return ","
