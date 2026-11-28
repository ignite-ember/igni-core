"""An OpenDocument text's headings, paragraphs and tables, in document order."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from igni_core.core.documents.budget import ReadBudget
from igni_core.core.documents.notes import DocumentNotes
from igni_core.core.documents.odf_package import OdfPackage
from igni_core.core.documents.odf_text import OdfText
from igni_core.core.documents.reader import DocumentReader
from igni_core.core.documents.schemas import DocumentText
from igni_core.core.documents.unit_run import UnitRun
from igni_core.core.documents.window import ReadWindow


class OdtDocumentReader(DocumentReader):
    """Read as a Word document is: headings as ``#`` lines, tables as Markdown, a part counting paragraphs."""

    extensions: ClassVar[frozenset[str]] = frozenset({".odt"})

    def _read(self, path: Path, budget: ReadBudget, window: ReadWindow) -> DocumentText:
        blocks = list(
            OdfText.blocks(
                OdfPackage.body(path, self.MAX_UNZIPPED_BYTES, self.MAX_PART_BYTES, "text")
            )
        )
        if window.past(len(blocks)):
            raise window.past_end(str(path), DocumentNotes.counted(len(blocks), "paragraph"))
        run = UnitRun.collect(
            len(blocks), lambda n: blocks[n - 1].render(budget, self.MAX_ROWS), budget, window
        )
        texts = [text for text in run.texts if text]
        return DocumentText(
            path=str(path),
            body="\n".join(texts),
            has_text=bool(texts),
            notes=budget.notes(window.continuation(budget, run.stopped)),
        )
