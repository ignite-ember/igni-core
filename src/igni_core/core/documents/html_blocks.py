"""An HTML or XHTML page as paragraphs, ``#`` headings and tables; scripts and styles dropped."""

from __future__ import annotations

from html.entities import name2codepoint
from typing import ClassVar

from lxml import etree

from igni_core.core.documents.safe_xml import SafeXml
from igni_core.core.documents.schemas import ContentBlock


class HtmlBlocks:
    """Walks a page once, flushing its running line at every block element."""

    _SKIPPED: ClassVar[frozenset[str]] = frozenset(
        {"head", "script", "style", "template", "noscript", "svg", "math", "iframe", "object"}
    )
    _BLOCKS: ClassVar[frozenset[str]] = frozenset(
        {
            "address", "article", "aside", "blockquote", "body", "caption", "dd", "details",
            "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "header",
            "hr", "html", "li", "main", "nav", "ol", "p", "section", "summary", "ul",
        }
    )  # fmt: skip
    _HEADINGS: ClassVar[dict[str, int]] = {f"h{n}": n for n in range(1, 7)}

    def __init__(self) -> None:
        self._blocks: list[ContentBlock] = []
        self._line: list[str] = []

    @classmethod
    def of(cls, root: etree._Element) -> list[ContentBlock]:
        """The blocks of the page under ``root``, in document order."""
        walker = cls()
        walker._walk(root)
        walker._flush()
        return walker._blocks

    def _walk(self, element: etree._Element) -> None:
        name = SafeXml.name(element)
        if isinstance(element, etree._Entity):
            self._line.append(self._entity(element))
        elif not name or name in self._SKIPPED:
            pass
        elif name in self._HEADINGS:
            self._flush()
            text = self._inline(element)
            if text:
                self._blocks.append(ContentBlock(text=f"{'#' * self._HEADINGS[name]} {text}"))
        elif name == "table":
            self._flush()
            self._blocks.append(ContentBlock(rows=self._rows(element)))
        elif name == "pre":
            self._flush()
            text = self._raw(element).strip("\n")
            if text.strip():
                self._blocks.append(ContentBlock(text=text))
        elif name == "br":
            self._line.append("\n")
        else:
            block = name in self._BLOCKS
            if block:
                self._flush()
            self._line.append(element.text or "")
            for child in element:
                self._walk(child)
            if block:
                self._flush()
        self._line.append(element.tail or "")

    def _flush(self) -> None:
        text = "\n".join(" ".join(line.split()) for line in "".join(self._line).split("\n"))
        self._line = []
        text = "\n".join(line for line in text.split("\n") if line)
        if text:
            self._blocks.append(ContentBlock(text=text))

    def _rows(self, table: etree._Element) -> list[list[str]]:
        rows: list[list[str]] = []
        for row in table.iter():
            if SafeXml.name(row) != "tr":
                continue
            cells = [cell for cell in row if SafeXml.name(cell) in ("td", "th")]
            rows.append([self._inline(cell) for cell in cells])
        return rows

    def _inline(self, element: etree._Element) -> str:
        return " ".join(self._raw(element).split())

    def _raw(self, element: etree._Element) -> str:
        """The element's text as written, entities named in HTML spelled out, skipped elements left out."""
        parts = [element.text or ""]
        for child in element:
            if isinstance(child, etree._Entity):
                parts.append(self._entity(child))
            elif SafeXml.name(child) == "br":
                parts.append("\n")
            elif SafeXml.name(child) and SafeXml.name(child) not in self._SKIPPED:
                parts.append(self._raw(child))
            parts.append(child.tail or "")
        return "".join(parts)

    @staticmethod
    def _entity(entity: etree._Entity) -> str:
        """An HTML entity's character; any other entity, declared in the file, is never expanded."""
        code = name2codepoint.get(entity.name)
        return chr(code) if code is not None else ""
