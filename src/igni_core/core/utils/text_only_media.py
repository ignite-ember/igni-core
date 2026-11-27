"""What a text-only model is told about the media files a message names.

A model without vision cannot take an image, a recording or a video, and
told only "read before responding" it tried for minutes: ``cat`` on a PNG,
OCR scripts, ASCII renderings. It is told instead that vision is disabled,
given each file's metadata, and asked to tell the person so.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from igni_core.core.utils.media_metadata import MediaMetadataReader
from igni_core.core.utils.media_schemas import MediaFileMetadata


class TextOnlyMediaNotice(BaseModel):
    """The media files of one turn for a text-only model, and the lines that say so."""

    #: The model-facing instruction ahead of the metadata lines.
    HEADER: ClassVar[str] = (
        "[Vision is disabled for this model. Do not open, read, decode, OCR or otherwise "
        "extract the content of these media files; only their metadata is available. "
        "Tell the user you cannot see them and suggest switching to a vision-enabled model.]"
    )
    _OPEN: ClassVar[str] = "<attached-files>\n"
    _CLOSE: ClassVar[str] = "\n</attached-files>"

    files: list[MediaFileMetadata] = Field(default_factory=list)

    @classmethod
    def collect(cls, paths: Iterable[str], base_dir: Path | None) -> TextOnlyMediaNotice:
        """The notice for the media files among ``paths``; relative ones are taken from ``base_dir``."""
        return cls(files=MediaMetadataReader(base_dir).read_all(paths))

    def block(self) -> str:
        """The instruction line followed by one metadata line per file."""
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

    def user_notice(self, model: str) -> str:
        """The line shown to the person: the model cannot see these files."""
        names = ", ".join(f.name for f in self.files)
        return (
            f"Vision is disabled for {model}: only the metadata of {names} was sent. "
            "Switch to a vision-enabled model to let it see the content."
        )
