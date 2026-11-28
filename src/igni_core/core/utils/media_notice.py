"""A header and one metadata line per media file, closing a message's ``<attached-files>`` block."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from igni_core.core.utils.media_schemas import MediaFileMetadata


class MediaNotice(BaseModel):
    """Media files a turn names, and the lines that tell the model about them."""

    HEADER: ClassVar[str] = ""
    """The model-facing line ahead of the metadata lines."""
    _OPEN: ClassVar[str] = "<attached-files>\n"
    _CLOSE: ClassVar[str] = "\n</attached-files>"

    files: list[MediaFileMetadata] = Field(default_factory=list)

    def block(self) -> str:
        """The header line followed by one metadata line per file."""
        return "\n".join([self.HEADER, *(f.describe() for f in self.files)])

    def wrap(self, message: str) -> str:
        """``message`` with the notice at the end of its ``<attached-files>`` block, made if absent."""
        if not self.files:
            return message
        if message.startswith(self._OPEN) and self._CLOSE in message:
            head, tail = message.split(self._CLOSE, 1)
            return f"{head}\n{self.block()}{self._CLOSE}{tail}"
        wrapper = f"{self._OPEN}{self.block()}{self._CLOSE}"
        return wrapper + ("\n" + message if message else "")
