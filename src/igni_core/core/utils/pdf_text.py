"""The text of a PDF, page by page, as pypdf extracts it."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field
from pypdf import PdfReader

logger = logging.getLogger(__name__)


class PdfText(BaseModel):
    """One PDF's extracted text, cut at :attr:`MAX_CHARS`."""

    #: About 25k tokens; a longer document is cut, and says so.
    MAX_CHARS: ClassVar[int] = 100_000

    path: str
    page_count: int = 0
    pages: list[str] = Field(default_factory=list)
    """The text of each page read, in order; fewer than ``page_count`` when cut."""
    truncated: bool = False

    @classmethod
    def read(cls, path: Path) -> PdfText:
        """The PDF's text; no pages when it cannot be read or has no text layer."""
        try:
            reader = PdfReader(path)
            if reader.is_encrypted:
                reader.decrypt("")
            pages: list[str] = []
            used = 0
            for page in reader.pages:
                text = (page.extract_text() or "").strip()
                if used + len(text) > cls.MAX_CHARS:
                    pages.append(text[: cls.MAX_CHARS - used])
                    return cls(
                        path=str(path), page_count=len(reader.pages), pages=pages, truncated=True
                    )
                pages.append(text)
                used += len(text)
            return cls(path=str(path), page_count=len(reader.pages), pages=pages)
        except Exception as exc:  # noqa: BLE001 — a PDF pypdf cannot read is reported, not raised
            logger.info("could not read the text of %s: %s", path, exc)
            return cls(path=str(path))

    @property
    def has_text(self) -> bool:
        """Whether any page gave text: a scanned PDF has none."""
        return any(self.pages)
