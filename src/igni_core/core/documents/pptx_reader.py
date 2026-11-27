"""A PowerPoint deck, slide by slide: title, text, tables and speaker notes."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import ClassVar

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.shapes.base import BaseShape
from pptx.shapes.graphfrm import GraphicFrame
from pptx.shapes.group import GroupShape
from pptx.slide import Slide

from igni_core.core.documents.budget import ReadBudget
from igni_core.core.documents.markdown_table import MarkdownTable
from igni_core.core.documents.notes import DocumentNotes
from igni_core.core.documents.ooxml_archive import OoxmlArchive
from igni_core.core.documents.reader import DocumentReader
from igni_core.core.documents.schemas import DocumentText


class PptxDocumentReader(DocumentReader):
    """Each slide under ``--- Slide n ---``; embedded objects and links are not opened."""

    extensions: ClassVar[frozenset[str]] = frozenset({".pptx"})

    def _read(self, path: Path, budget: ReadBudget) -> DocumentText:
        OoxmlArchive.check(path, self.MAX_UNZIPPED_BYTES)
        slides = list(Presentation(str(path)).slides)
        parts: list[str] = []
        has_text = False
        for n, slide in enumerate(slides[: self.MAX_SLIDES], start=1):
            content = self._slide(slide, budget)
            has_text = has_text or bool(content)
            parts.append(f"--- Slide {n} ---" + (f"\n{content}" if content else ""))
            if budget.spent:
                break
        notes = budget.notes()
        if len(slides) > self.MAX_SLIDES and not budget.spent:
            notes.append(DocumentNotes.parts_cut("Slides", self.MAX_SLIDES, len(slides)))
        return DocumentText(
            path=str(path),
            count=DocumentNotes.counted(len(slides), "slide"),
            body="\n".join(parts),
            has_text=has_text,
            notes=notes,
        )

    def _slide(self, slide: Slide, budget: ReadBudget) -> str:
        title = slide.shapes.title
        lines: list[str] = []
        if title is not None and title.text_frame.text.strip():
            lines.append(budget.take(f"# {title.text_frame.text.strip()}"))
        for shape in self._shapes(slide.shapes):
            if budget.spent:
                break
            if title is not None and shape.shape_id == title.shape_id:
                continue
            text = self._shape(shape, budget)
            if text:
                lines.append(text)
        if not budget.spent and slide.has_notes_slide:
            frame = slide.notes_slide.notes_text_frame
            spoken = frame.text.strip() if frame is not None else ""
            if spoken:
                lines.append(budget.take(f"Speaker notes:\n{spoken}"))
        return "\n".join(line for line in lines if line)

    def _shapes(self, shapes: Iterable[BaseShape]) -> Iterator[BaseShape]:
        for shape in shapes:
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP and isinstance(shape, GroupShape):
                yield from self._shapes(shape.shapes)
            else:
                yield shape

    def _shape(self, shape: BaseShape, budget: ReadBudget) -> str:
        if isinstance(shape, GraphicFrame) and shape.has_table:
            rows = ([cell.text for cell in row.cells] for row in shape.table.rows)
            return MarkdownTable.collect(rows, self.MAX_ROWS, budget).render(budget)
        if shape.has_text_frame:
            text = shape.text_frame.text.strip()
            return budget.take(text) if text else ""
        return ""
