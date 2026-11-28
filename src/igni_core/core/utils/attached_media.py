"""What a vision model is told about the media files attached to a message as content."""

from __future__ import annotations

from typing import ClassVar

from igni_core.core.utils.media_notice import MediaNotice


class AttachedMediaNotice(MediaNotice):
    """Media files a vision model was sent as content, so it does not open them with tools."""

    HEADER: ClassVar[str] = (
        "[These media files are attached to this message and already visible to you. "
        "Do not open, read, decode, OCR or otherwise extract their content with tools.]"
    )
