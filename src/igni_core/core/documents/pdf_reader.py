"""A PDF's text, page by page, as pypdf extracts it."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pypdf import PdfReader

from igni_core.core.documents.budget import ReadBudget
from igni_core.core.documents.notes import DocumentNotes
from igni_core.core.documents.reader import DocumentReader
from igni_core.core.documents.schemas import DocumentText


class PdfDocumentReader(DocumentReader):
    """Each page's text under ``--- Page n ---``; a scanned PDF has none."""

    extensions: ClassVar[frozenset[str]] = frozenset({".pdf"})

    def _read(self, path: Path, budget: ReadBudget) -> DocumentText:
        reader = PdfReader(path)
        if reader.is_encrypted:
            reader.decrypt("")
        pages: list[str] = []
        for page in reader.pages:
            text = (page.extract_text() or "").strip()
            taken = budget.take(text)
            if budget.late:
                break
            pages.append(taken)
            if budget.spent:
                break
        return DocumentText(
            path=str(path),
            count=DocumentNotes.counted(len(reader.pages), "page"),
            body="\n".join(f"--- Page {n} ---\n{text}" for n, text in enumerate(pages, start=1)),
            has_text=any(pages),
            notes=budget.notes(),
        )
