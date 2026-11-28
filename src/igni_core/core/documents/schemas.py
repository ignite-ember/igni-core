"""What one document gives the model."""

from __future__ import annotations

from pydantic import BaseModel, Field


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
