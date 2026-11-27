"""What one turn's ``<attached-files>`` block says about the media files the message names."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, Field

from igni_core.core.utils.attached_media import AttachedMediaNotice
from igni_core.core.utils.media_metadata import MediaMetadataReader
from igni_core.core.utils.text_only_media import TextOnlyMediaNotice
from igni_core.core.utils.unattached_media import UnattachedMediaNotice


class TurnMediaNotices(BaseModel):
    """The media files a turn names: attached, not attached, or unseen by a text-only model."""

    attached: AttachedMediaNotice = Field(default_factory=AttachedMediaNotice)
    unattached: UnattachedMediaNotice = Field(default_factory=UnattachedMediaNotice)
    unseen: TextOnlyMediaNotice = Field(default_factory=TextOnlyMediaNotice)

    @classmethod
    def collect(
        cls, named: Iterable[str], attached: Iterable[str], base_dir: Path | None, *, vision: bool
    ) -> TurnMediaNotices:
        """Each media file in ``named`` as attached when ``attached`` holds its path, else as not seen."""
        files = MediaMetadataReader(base_dir).read_all(named)
        sent = set(attached)
        missing = [f for f in files if f.path not in sent]
        return cls(
            attached=AttachedMediaNotice(files=[f for f in files if f.path in sent]),
            unattached=UnattachedMediaNotice(files=missing if vision else []),
            unseen=TextOnlyMediaNotice(files=[] if vision else missing),
        )

    def user_notices(self, model: str) -> list[str]:
        """The lines shown to the person about the media files ``model`` cannot see."""
        notices = [self.unattached.user_notice()] if self.unattached.files else []
        if self.unseen.files:
            notices.append(self.unseen.user_notice(model))
        return notices

    def wrap(self, message: str) -> str:
        """``message`` with the attached, the not-attached and the unseen files' notices, in order."""
        return self.unseen.wrap(self.unattached.wrap(self.attached.wrap(message)))
