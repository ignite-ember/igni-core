"""The text of the documents a message attaches, quarantined in its ``<attached-files>`` block."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from igni_core.core.documents.notes import DocumentNotes
from igni_core.core.documents.registry import DocumentReaders
from igni_core.core.documents.schemas import DocumentText
from igni_core.core.hooks.untrusted import wrap_untrusted


class AttachedDocumentText(BaseModel):
    """The documents of one message, and the block lines that carry their text."""

    _OPEN: ClassVar[str] = "<attached-files>\n"
    _CLOSE: ClassVar[str] = "\n</attached-files>"
    _WRAPPER_TAG: ClassVar[re.Pattern[str]] = re.compile(
        r"<(?=\s*/?\s*(?:system-context|attached-files|extension-message|loop-iteration)\b)",
        re.IGNORECASE,
    )
    """The ``<`` opening one of igni's wrapper tags, written ``&lt;`` inside a document."""

    documents: list[DocumentText] = Field(default_factory=list)

    @classmethod
    def collect(
        cls, paths: Iterable[str], readers: DocumentReaders | None = None
    ) -> AttachedDocumentText:
        """The text of each existing document among ``paths``, once each."""
        readers = readers or DocumentReaders.default()
        documents = [readers.read(Path(p)) for p in dict.fromkeys(paths) if Path(p).is_file()]
        return cls(documents=[d for d in documents if d is not None])

    def wrap(self, message: str) -> str:
        """``message`` with the documents' text at the end of its ``<attached-files>`` block."""
        if not self.documents:
            return message
        block = "\n".join(self._lines(document) for document in self.documents)
        if message.startswith(self._OPEN) and self._CLOSE in message:
            head, tail = message.split(self._CLOSE, 1)
            return f"{head}\n{block}{self._CLOSE}{tail}"
        return f"{self._OPEN}{block}{self._CLOSE}" + ("\n" + message if message else "")

    def _lines(self, document: DocumentText) -> str:
        if document.note:
            return document.note
        if not document.has_text:
            return DocumentNotes.no_text(document.path)
        body = "\n".join([self._WRAPPER_TAG.sub("&lt;", document.body), *document.notes])
        head = DocumentNotes.extracted(document.path, document.count)
        return f"{head}\n{wrap_untrusted(body)}"
