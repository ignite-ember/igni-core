"""The text of the documents a message attaches, quarantined in its ``<attached-files>`` block."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from igni_core.core.documents.block import DocumentBlock
from igni_core.core.documents.registry import DocumentReaders
from igni_core.core.documents.schemas import DocumentText


class AttachedDocumentText(BaseModel):
    """The documents of one message, and the block lines that carry their text."""

    _OPEN: ClassVar[str] = "<attached-files>\n"
    _CLOSE: ClassVar[str] = "\n</attached-files>"

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
        block = "\n".join(DocumentBlock.lines(document) for document in self.documents)
        if message.startswith(self._OPEN) and self._CLOSE in message:
            head, tail = message.split(self._CLOSE, 1)
            return f"{head}\n{block}{self._CLOSE}{tail}"
        return f"{self._OPEN}{block}{self._CLOSE}" + ("\n" + message if message else "")
