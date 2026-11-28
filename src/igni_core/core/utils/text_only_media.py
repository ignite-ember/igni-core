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

from igni_core.core.utils.media_metadata import MediaMetadataReader
from igni_core.core.utils.media_notice import MediaNotice


class TextOnlyMediaNotice(MediaNotice):
    """The media files of one turn for a text-only model, and the lines that say so."""

    HEADER: ClassVar[str] = (
        "[Vision is disabled for this model. Do not open, read, decode, OCR or otherwise "
        "extract the content of these media files; only their metadata is available. "
        "Tell the user you cannot see them and suggest switching to a vision-enabled model.]"
    )

    @classmethod
    def collect(cls, paths: Iterable[str], base_dir: Path | None) -> TextOnlyMediaNotice:
        """The notice for the media files among ``paths``; relative ones are taken from ``base_dir``."""
        return cls(files=MediaMetadataReader(base_dir).read_all(paths))

    def user_notice(self, model: str) -> str:
        """The line shown to the person: the model cannot see these files."""
        names = ", ".join(f.name for f in self.files)
        return (
            f"Vision is disabled for {model}: only the metadata of {names} was sent. "
            "Switch to a vision-enabled model to let it see the content."
        )
