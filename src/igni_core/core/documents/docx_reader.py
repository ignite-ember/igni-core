"""A Word document's headings, paragraphs and tables, in document order."""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from igni_core.core.documents.budget import ReadBudget
from igni_core.core.documents.markdown_table import MarkdownTable
from igni_core.core.documents.ooxml_archive import OoxmlArchive
from igni_core.core.documents.reader import DocumentReader
from igni_core.core.documents.schemas import DocumentText


class DocxDocumentReader(DocumentReader):
    """Headings as ``#`` lines, tables as Markdown; embedded objects and links are not opened."""

    extensions: ClassVar[frozenset[str]] = frozenset({".docx"})
    _HEADING: ClassVar[re.Pattern[str]] = re.compile(r"Heading (\d)")

    def _read(self, path: Path, budget: ReadBudget) -> DocumentText:
        OoxmlArchive.check(path, self.MAX_UNZIPPED_BYTES)
        blocks: list[str] = []
        for block in Document(str(path)).iter_inner_content():
            text = self._block(block, budget)
            if text:
                blocks.append(text)
            if budget.spent:
                break
        return DocumentText(
            path=str(path), body="\n".join(blocks), has_text=bool(blocks), notes=budget.notes()
        )

    def _block(self, block: Paragraph | Table, budget: ReadBudget) -> str:
        if isinstance(block, Table):
            rows = ([cell.text for cell in row.cells] for row in block.rows)
            return MarkdownTable.collect(rows, self.MAX_ROWS, budget).render(budget)
        return budget.take(self._paragraph(block))

    def _paragraph(self, paragraph: Paragraph) -> str:
        text = paragraph.text.strip()
        style = paragraph.style.name if paragraph.style is not None else ""
        if not text or not style:
            return text
        if style == "Title":
            return f"# {text}"
        heading = self._HEADING.fullmatch(style)
        return f"{'#' * int(heading.group(1))} {text}" if heading else text
