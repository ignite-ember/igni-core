"""Which reader reads which file, by its extension."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from igni_core.core.documents.csv_reader import CsvDocumentReader
from igni_core.core.documents.docx_reader import DocxDocumentReader
from igni_core.core.documents.legacy_reader import LegacyDocumentReader
from igni_core.core.documents.pdf_reader import PdfDocumentReader
from igni_core.core.documents.pptx_reader import PptxDocumentReader
from igni_core.core.documents.reader import DocumentReader
from igni_core.core.documents.schemas import DocumentText
from igni_core.core.documents.window import ReadWindow
from igni_core.core.documents.xlsx_reader import XlsxDocumentReader


class DocumentReaders:
    """One reader per extension; a file no reader claims is not a document."""

    def __init__(self, readers: Sequence[DocumentReader]) -> None:
        self._by_extension = {ext: reader for reader in readers for ext in reader.extensions}

    @classmethod
    def default(cls) -> DocumentReaders:
        """Every format igni reads, and the legacy ones it names."""
        return cls(
            [
                PdfDocumentReader(),
                CsvDocumentReader(),
                XlsxDocumentReader(),
                DocxDocumentReader(),
                PptxDocumentReader(),
                LegacyDocumentReader(),
            ]
        )

    @property
    def extensions(self) -> frozenset[str]:
        """Every extension a reader claims, with its dot, lower case."""
        return frozenset(self._by_extension)

    def read(self, path: Path, window: ReadWindow | None = None) -> DocumentText | None:
        """The document's text in ``window`` or its note; ``None`` when no reader claims the file."""
        reader = self._by_extension.get(path.suffix.lower())
        return reader.read(path, window) if reader is not None else None
