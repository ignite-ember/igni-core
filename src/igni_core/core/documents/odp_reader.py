"""An OpenDocument presentation, slide by slide: title, text, tables and speaker notes."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

from lxml import etree

from igni_core.core.documents.budget import ReadBudget
from igni_core.core.documents.notes import DocumentNotes
from igni_core.core.documents.odf_package import OdfPackage
from igni_core.core.documents.odf_text import OdfText
from igni_core.core.documents.reader import DocumentReader
from igni_core.core.documents.schemas import DocumentText
from igni_core.core.documents.window import ReadWindow


class OdpDocumentReader(DocumentReader):
    """Each slide under ``--- Slide n ---``, as a PowerPoint deck's; embedded objects are not opened."""

    extensions: ClassVar[frozenset[str]] = frozenset({".odp"})
    _CLASS: ClassVar[str] = OdfText.tag(OdfText.PRESENTATION, "class")
    _NOTES: ClassVar[str] = OdfText.tag(OdfText.PRESENTATION, "notes")
    _GROUP: ClassVar[str] = OdfText.tag(OdfText.DRAW, "g")

    def _read(self, path: Path, budget: ReadBudget, window: ReadWindow) -> DocumentText:
        body = OdfPackage.body(path, self.MAX_UNZIPPED_BYTES, self.MAX_PART_BYTES, "presentation")
        slides = body.findall(OdfText.tag(OdfText.DRAW, "page"))
        count = DocumentNotes.counted(len(slides), "slide")
        if window.past(len(slides)):
            raise window.past_end(str(path), count)
        end = window.end(self.MAX_SLIDES, len(slides))
        parts: list[str] = []
        has_text = False
        stopped = 0
        for n in range(window.first, end + 1):
            content = self._slide(slides[n - 1], budget)
            has_text = has_text or bool(content)
            parts.append(f"--- Slide {n} ---" + (f"\n{content}" if content else ""))
            if budget.spent:
                stopped = n
                break
        notes = budget.notes(window.continuation(budget, stopped))
        if window.capped(self.MAX_SLIDES, len(slides)) and not budget.spent:
            cut = DocumentNotes.parts_cut("Slides", end, len(slides))
            notes.append(window.note(cut, window.after(end)))
        return DocumentText(
            path=str(path), count=count, body="\n".join(parts), has_text=has_text, notes=notes
        )

    def _slide(self, slide: etree._Element, budget: ReadBudget) -> str:
        shapes = list(self._shapes(slide))
        titles = [shape for shape in shapes if shape.get(self._CLASS) == "title"]
        lines: list[str] = []
        for title in titles[:1]:
            text = self._text(title)
            if text:
                lines.append(budget.take(f"# {text}"))
        for shape in shapes:
            if budget.spent:
                break
            if titles and shape is titles[0]:
                continue
            lines.extend(block.render(budget, self.MAX_ROWS) for block in OdfText.blocks(shape))
        notes = slide.find(self._NOTES)
        if not budget.spent and notes is not None:
            spoken = "\n".join(self._text(shape) for shape in self._shapes(notes))
            if spoken.strip():
                lines.append(budget.take(f"Speaker notes:\n{spoken.strip()}"))
        return "\n".join(line for line in lines if line)

    def _shapes(self, parent: etree._Element) -> Iterator[etree._Element]:
        """The slide's shapes, groups opened, its notes page left out."""
        for child in parent:
            if child.tag == self._GROUP:
                yield from self._shapes(child)
            elif isinstance(child.tag, str) and child.tag.startswith(f"{{{OdfText.DRAW}}}"):
                yield child

    def _text(self, shape: etree._Element) -> str:
        """A shape's paragraphs, one per line; the notes page's slide thumbnail has none."""
        if shape.get(self._CLASS) not in (None, "title", "notes", "subtitle", "outline", "text"):
            return ""
        return "\n".join(block.text for block in OdfText.blocks(shape) if block.rows is None)
