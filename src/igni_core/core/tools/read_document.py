"""ReadDocumentTools — the model reads a PDF, Word, Excel, PowerPoint, CSV or TSV file as text."""

from __future__ import annotations

import asyncio
import codecs
from pathlib import Path
from typing import Any, ClassVar

from agno.tools import Toolkit

from igni_core.core.documents.block import DocumentBlock
from igni_core.core.documents.exceptions import DocumentRefused
from igni_core.core.documents.notes import DocumentNotes
from igni_core.core.documents.registry import DocumentReaders
from igni_core.core.documents.window import ReadWindow
from igni_core.core.tools.document_access import DocumentAccess


class ReadDocumentTools(Toolkit):
    """The attachment readers, for any document in the project the model decides to read."""

    _SNIFF_BYTES: ClassVar[int] = 8192
    """How much of an unclaimed file is looked at to tell text from binary."""

    def __init__(
        self,
        base_dir: str | None = None,
        *,
        requires_confirmation_tools: list[str] | None = None,
        **toolkit_kwargs: Any,
    ):
        super().__init__(name="igni_read_document", **toolkit_kwargs)
        self.base_dir = Path(base_dir) if base_dir else Path.cwd()
        self._readers = DocumentReaders.default()
        self.register(self.read_document)
        if requires_confirmation_tools:
            self.requires_confirmation_tools = requires_confirmation_tools
            for registry in (self.functions, self.async_functions):
                for name, func in registry.items():
                    if name in requires_confirmation_tools:
                        func.requires_confirmation = True

    async def read_document(self, path: str, part: str = "") -> str:
        """Read a document that is not plain text: PDF, Word (.docx), Excel (.xlsx, .xlsm) or PowerPoint (.pptx). CSV and TSV files are read too, as a table.

        Plain-text and code files are not for this tool: read them with the shell, as before. A long document is cut, and the cut says which part to ask for next.

        Args:
            path: The document's path, absolute or relative to the project. Only files inside the project are read.
            part: Which part to read, as a range: "21-40", "21-" (to the end) or "21". It counts pages in a PDF, slides in a deck, sheets in a workbook, rows in a CSV or TSV, and paragraphs in a Word document. For rows of one sheet, write the sheet's name first: "Sales!501-". Leave it empty to read from the start.
        """
        return await asyncio.to_thread(self._read, path, part)

    def _read(self, raw: str, part: str) -> str:
        """The document's text block, or the note that stands in for it."""
        try:
            target = DocumentAccess(self.base_dir).check(raw)
            window = ReadWindow.parse(part, str(target))
            document = self._readers.read(target, window)
            if document is None:
                raise DocumentRefused(self._unclaimed(target))
        except DocumentRefused as refused:
            return refused.note
        except (OSError, RuntimeError, ValueError):
            return DocumentNotes.no_text(raw)
        return DocumentBlock.lines(document)

    def _unclaimed(self, path: Path) -> str:
        """The note for a file no reader claims: plain text, or a format this tool does not read."""
        with path.open("rb") as handle:
            head = handle.read(self._SNIFF_BYTES)
        if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) or b"\0" not in head:
            return DocumentNotes.plain_text(str(path))
        return DocumentNotes.unsupported(str(path))


__all__ = ["ReadDocumentTools"]
