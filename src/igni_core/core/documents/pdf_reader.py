"""A PDF's text, page by page, as pypdf extracts it."""

from __future__ import annotations

from pathlib import Path
from typing import IO, ClassVar

from pypdf import PdfReader

from igni_core.core.documents.budget import ReadBudget
from igni_core.core.documents.notes import DocumentNotes
from igni_core.core.documents.reader import DocumentReader
from igni_core.core.documents.schemas import DocumentText
from igni_core.core.documents.window import ReadWindow


class PdfDocumentReader(DocumentReader):
    """Each page's text under ``--- Page n ---``; a scanned PDF has none."""

    extensions: ClassVar[frozenset[str]] = frozenset({".pdf"})

    def _read(self, path: Path, budget: ReadBudget, window: ReadWindow) -> DocumentText:
        return self.pages(path, str(path), budget, window)

    def pages(
        self, source: Path | IO[bytes], path: str, budget: ReadBudget, window: ReadWindow
    ) -> DocumentText:
        """The text of the PDF in ``source``, named ``path`` to the model; may raise."""
        reader = PdfReader(source)
        if reader.is_encrypted:
            reader.decrypt("")
        total = len(reader.pages)
        count = DocumentNotes.counted(total, "page")
        if window.past(total):
            raise window.past_end(path, count)
        pages: list[str] = []
        stopped = 0
        for n in range(window.first, window.end(total, total) + 1):
            text = (reader.pages[n - 1].extract_text() or "").strip()
            taken = budget.take(text)
            if budget.late:
                stopped = n
                break
            pages.append(taken)
            if budget.spent:
                stopped = n
                break
        return DocumentText(
            path=path,
            count=count,
            body="\n".join(
                f"--- Page {n} ---\n{text}" for n, text in enumerate(pages, start=window.first)
            ),
            has_text=any(pages),
            notes=budget.notes(window.continuation(budget, stopped)),
        )
