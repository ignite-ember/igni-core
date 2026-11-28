"""The text of the PDFs a message attaches, quarantined in its ``<attached-files>`` block."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from igni_core.core.hooks.untrusted import wrap_untrusted
from igni_core.core.utils.pdf_text import PdfText


class AttachedPdfText(BaseModel):
    """The PDFs of one message, and the block lines that carry their text."""

    _OPEN: ClassVar[str] = "<attached-files>\n"
    _CLOSE: ClassVar[str] = "\n</attached-files>"
    _WRAPPER_TAG: ClassVar[re.Pattern[str]] = re.compile(
        r"<(?=\s*/?\s*(?:system-context|attached-files|extension-message|loop-iteration)\b)",
        re.IGNORECASE,
    )
    """The ``<`` opening one of igni's wrapper tags, written ``&lt;`` inside a document."""

    pdfs: list[PdfText] = Field(default_factory=list)

    @classmethod
    def collect(cls, paths: Iterable[str]) -> AttachedPdfText:
        """The text of each existing ``.pdf`` among ``paths``, once each."""
        seen = dict.fromkeys(p for p in paths if p.lower().endswith(".pdf"))
        return cls(pdfs=[PdfText.read(Path(p)) for p in seen if Path(p).is_file()])

    def wrap(self, message: str) -> str:
        """``message`` with the PDFs' text at the end of its ``<attached-files>`` block."""
        if not self.pdfs:
            return message
        block = "\n".join(self._lines(pdf) for pdf in self.pdfs)
        if message.startswith(self._OPEN) and self._CLOSE in message:
            head, tail = message.split(self._CLOSE, 1)
            return f"{head}\n{block}{self._CLOSE}{tail}"
        return f"{self._OPEN}{block}{self._CLOSE}" + ("\n" + message if message else "")

    def _lines(self, pdf: PdfText) -> str:
        if not pdf.has_text:
            return f"[No text could be extracted from {pdf.path}.]"
        pages = "\n".join(
            f"--- Page {n} ---\n{self._WRAPPER_TAG.sub('&lt;', text)}"
            for n, text in enumerate(pdf.pages, start=1)
        )
        if pdf.truncated:
            pages += f"\n[Text truncated at {PdfText.MAX_CHARS} characters.]"
        count = f"{pdf.page_count} page" + ("" if pdf.page_count == 1 else "s")
        return f"[Text extracted from {pdf.path} ({count}):]\n{wrap_untrusted(pages)}"
