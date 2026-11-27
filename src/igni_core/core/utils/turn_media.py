"""What one turn's ``<attached-files>`` block says about the media files the message names."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, Field

from igni_core.core.utils.attached_media import AttachedMediaNotice
from igni_core.core.utils.media_metadata import MediaMetadataReader
from igni_core.core.utils.text_only_media import TextOnlyMediaNotice


class TurnMediaNotices(BaseModel):
    """The media files a turn names: those attached as content, and those the model cannot see."""

    attached: AttachedMediaNotice = Field(default_factory=AttachedMediaNotice)
    unseen: TextOnlyMediaNotice = Field(default_factory=TextOnlyMediaNotice)

    @classmethod
    def collect(
        cls, named: Iterable[str], attached: Iterable[str], base_dir: Path | None
    ) -> TurnMediaNotices:
        """Each media file in ``named`` as attached when ``attached`` holds its path, else as unseen."""
        files = MediaMetadataReader(base_dir).read_all(named)
        sent = set(attached)
        return cls(
            attached=AttachedMediaNotice(files=[f for f in files if f.path in sent]),
            unseen=TextOnlyMediaNotice(files=[f for f in files if f.path not in sent]),
        )

    def wrap(self, message: str) -> str:
        """``message`` with the attached files' notice, then the unseen files', closing its block."""
        return self.unseen.wrap(self.attached.wrap(message))
