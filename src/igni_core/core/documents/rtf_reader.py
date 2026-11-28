"""An RTF document's text, paragraph by paragraph, as striprtf gives it."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from striprtf.striprtf import rtf_to_text

from igni_core.core.documents.budget import ReadBudget
from igni_core.core.documents.exceptions import DocumentRefused
from igni_core.core.documents.notes import DocumentNotes
from igni_core.core.documents.reader import DocumentReader
from igni_core.core.documents.schemas import DocumentText
from igni_core.core.documents.unit_run import UnitRun
from igni_core.core.documents.window import ReadWindow


class RtfDocumentReader(DocumentReader):
    """Pictures, embedded objects and fields' code dropped; a part counts paragraphs."""

    extensions: ClassVar[frozenset[str]] = frozenset({".rtf"})
    MAX_RTF_BYTES: ClassVar[int] = 20 * 1024 * 1024
    """striprtf cannot be stopped midway, so a larger file is not handed to it."""

    def _read(self, path: Path, budget: ReadBudget, window: ReadWindow) -> DocumentText:
        if path.stat().st_size > self.MAX_RTF_BYTES:
            raise DocumentRefused(DocumentNotes.too_large(str(path)))
        text = rtf_to_text(path.read_bytes().decode("latin-1"), errors="ignore")
        paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
        if window.past(len(paragraphs)):
            raise window.past_end(str(path), DocumentNotes.counted(len(paragraphs), "paragraph"))
        run = UnitRun.collect(
            len(paragraphs), lambda n: budget.take(paragraphs[n - 1]), budget, window
        )
        texts = [text for text in run.texts if text]
        return DocumentText(
            path=str(path),
            body="\n".join(texts),
            has_text=bool(texts),
            notes=budget.notes(window.continuation(budget, run.stopped)),
        )
