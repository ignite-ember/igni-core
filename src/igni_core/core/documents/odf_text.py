"""The text of an OpenDocument part: its paragraphs, headings and tables, as the reader shows them."""

from __future__ import annotations

from collections.abc import Iterator
from typing import ClassVar

from lxml import etree

from igni_core.core.documents.schemas import ContentBlock


class OdfText:
    """OpenDocument's namespaces, and the walk from its elements to text."""

    TEXT: ClassVar[str] = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
    TABLE: ClassVar[str] = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
    OFFICE: ClassVar[str] = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
    DRAW: ClassVar[str] = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
    PRESENTATION: ClassVar[str] = "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"
    _NOT_TEXT: ClassVar[frozenset[str]] = frozenset(
        {
            f"{{{TEXT}}}note",
            f"{{{OFFICE}}}annotation",
            f"{{{TEXT}}}tracked-changes",
            f"{{{TEXT}}}sequence-decls",
            f"{{{OFFICE}}}forms",
        }
    )
    """Footnotes, comments, the record of deleted text, and declarations: never shown inline."""
    _CONTAINERS: ClassVar[frozenset[str]] = frozenset(
        {
            f"{{{TEXT}}}section",
            f"{{{TEXT}}}list",
            f"{{{TEXT}}}list-item",
            f"{{{TEXT}}}list-header",
            f"{{{TEXT}}}index-body",
            f"{{{TEXT}}}table-of-content",
            f"{{{TEXT}}}alphabetical-index",
            f"{{{TEXT}}}illustration-index",
            f"{{{TEXT}}}table-index",
            f"{{{TEXT}}}user-index",
            f"{{{TEXT}}}bibliography",
            f"{{{DRAW}}}frame",
            f"{{{DRAW}}}text-box",
            f"{{{DRAW}}}g",
        }
    )
    """Elements whose children are blocks of their own."""

    @classmethod
    def tag(cls, namespace: str, name: str) -> str:
        """The Clark name lxml gives ``name`` in ``namespace``."""
        return f"{{{namespace}}}{name}"

    @classmethod
    def blocks(cls, parent: etree._Element) -> Iterator[ContentBlock]:
        """``parent``'s headings, paragraphs and tables in order, lists and sections opened."""
        for child in parent:
            if child.tag == cls.tag(cls.TEXT, "h"):
                text = cls.inline(child)
                level = int(child.get(cls.tag(cls.TEXT, "outline-level")) or 1)
                if text:
                    yield ContentBlock(text=f"{'#' * min(max(level, 1), 6)} {text}")
            elif child.tag == cls.tag(cls.TEXT, "p"):
                text = cls.inline(child)
                if text and child.get(cls.tag(cls.TEXT, "style-name")) == "Title":
                    text = f"# {text}"
                if text:
                    yield ContentBlock(text=text)
            elif child.tag == cls.tag(cls.TABLE, "table"):
                yield ContentBlock(rows=[cls.row_cells(row) for row in cls.rows(child)])
            elif child.tag in cls._CONTAINERS:
                yield from cls.blocks(child)

    @classmethod
    def rows(cls, table: etree._Element) -> Iterator[etree._Element]:
        """``table``'s rows, inside header rows and row groups too, never a nested table's."""
        for child in table:
            if child.tag == cls.tag(cls.TABLE, "table-row"):
                yield child
            elif child.tag in (
                cls.tag(cls.TABLE, "table-header-rows"),
                cls.tag(cls.TABLE, "table-rows"),
                cls.tag(cls.TABLE, "table-row-group"),
            ):
                yield from cls.rows(child)

    @classmethod
    def cells(cls, row: etree._Element) -> Iterator[etree._Element]:
        """``row``'s cells, merged-over ones included so the columns line up."""
        for child in row:
            if child.tag in (
                cls.tag(cls.TABLE, "table-cell"),
                cls.tag(cls.TABLE, "covered-table-cell"),
            ):
                yield child

    @classmethod
    def row_cells(cls, row: etree._Element) -> list[str]:
        """Each cell's text, once per cell as written (a table in a document repeats nothing)."""
        return [cls.cell(cell) for cell in cls.cells(row)]

    @classmethod
    def cell(cls, cell: etree._Element) -> str:
        """A cell's paragraphs, one line each."""
        return "\n".join(block.text for block in cls.blocks(cell) if block.rows is None)

    @classmethod
    def inline(cls, element: etree._Element) -> str:
        """A paragraph's text: spaces, tabs and line breaks as written; notes and comments left out."""
        parts = [element.text or ""]
        for child in element:
            if child.tag == cls.tag(cls.TEXT, "s"):
                parts.append(" " * min(int(child.get(cls.tag(cls.TEXT, "c")) or 1), 100))
            elif child.tag == cls.tag(cls.TEXT, "tab"):
                parts.append("\t")
            elif child.tag == cls.tag(cls.TEXT, "line-break"):
                parts.append("\n")
            elif isinstance(child.tag, str) and child.tag not in cls._NOT_TEXT:
                parts.append(cls.inline(child))
            parts.append(child.tail or "")
        return "".join(parts).strip()
