"""What one document gives the model, and the blocks a reader builds it from."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from igni_core.core.documents.budget import ReadBudget
from igni_core.core.documents.markdown_table import MarkdownTable


class DocumentText(BaseModel):
    """One document's text, or the note that stands in for it."""

    path: str
    count: str = ""
    """The document's length in its own unit, such as ``2 pages``; empty when it has none."""
    body: str = ""
    has_text: bool = False
    notes: list[str] = Field(default_factory=list)
    """Notes after the text, such as where it was cut."""
    note: str = ""
    """The note shown instead of the text: password-protected, too large, a legacy format."""


class ContentBlock(BaseModel):
    """A paragraph or a table of a flowing document: OpenDocument, EPUB or an HTML email."""

    text: str = ""
    rows: list[list[str]] | None = None
    """The table's rows, the first being its header; ``None`` for a paragraph."""

    def render(self, budget: ReadBudget, max_rows: int) -> str:
        """The block within ``budget``: a paragraph as it is, a table in Markdown."""
        if self.rows is None:
            return budget.take(self.text) if self.text else ""
        return MarkdownTable.collect(self.rows, max_rows, budget).render(budget)


class NotebookOutput(BaseModel):
    """One output of a notebook cell, as the file stores it; unknown keys are ignored."""

    output_type: str = ""
    text: Any = ""
    data: dict[str, Any] = Field(default_factory=dict)
    """The output in each MIME type it was saved as."""
    ename: str = ""
    evalue: str = ""


class NotebookCell(BaseModel):
    """One notebook cell, as the file stores it; unknown keys are ignored."""

    cell_type: str = "raw"
    source: Any = None
    input: Any = None
    """A version 3 code cell's source."""
    outputs: list[Any] = Field(default_factory=list)
    attachments: Any = None
    """Images embedded in a markdown cell."""

    @staticmethod
    def joined(value: Any) -> str:
        """A notebook string, which may be stored as a list of lines."""
        if isinstance(value, list):
            return "".join(str(line) for line in value)
        return str(value) if value is not None else ""

    @property
    def code(self) -> str:
        """The cell's source text."""
        return self.joined(self.source if self.source is not None else self.input).strip("\n")
