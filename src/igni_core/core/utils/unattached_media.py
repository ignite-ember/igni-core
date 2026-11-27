"""What a vision model is told about the media files a message names that could not be attached."""

from __future__ import annotations

from typing import ClassVar

from igni_core.core.utils.media_notice import MediaNotice


class UnattachedMediaNotice(MediaNotice):
    """Media files a vision model was not sent as content, so only their metadata is available."""

    HEADER: ClassVar[str] = (
        "[These media files could not be attached, so you cannot see them. Do not open, read, "
        "decode, OCR or otherwise extract their content; only their metadata is available. "
        "Tell the user they were not attached.]"
    )

    def user_notice(self) -> str:
        """The line shown to the person: these files were not attached."""
        names = ", ".join(f.name for f in self.files)
        return f"{names} could not be attached, so only their metadata was sent."
