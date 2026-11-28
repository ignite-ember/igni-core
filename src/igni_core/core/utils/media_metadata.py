"""The metadata of a media file a text-only model is told about instead of its content."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import ClassVar

from igni_core.core.utils.image_dimensions import ImageDimensions
from igni_core.core.utils.media_schemas import MediaFileMetadata, MediaKind, MediaTaxonomy


class MediaMetadataReader:
    """Describes image, audio and video files by name, path, type, size and pixel size."""

    #: The media type each extension is reported as. Written out rather than
    #: asked of :mod:`mimetypes`, whose table differs between Python releases.
    MEDIA_TYPES: ClassVar[Mapping[str, str]] = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
        ".svg": "image/svg+xml",
        ".avif": "image/avif",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".aac": "audio/aac",
        ".m4a": "audio/mp4",
        ".wma": "audio/x-ms-wma",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".avi": "video/x-msvideo",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
        ".wmv": "video/x-ms-wmv",
    }

    def __init__(self, base_dir: Path | None = None, taxonomy: MediaTaxonomy | None = None) -> None:
        self._base_dir = base_dir
        self._taxonomy = taxonomy or MediaTaxonomy.default()

    def read(self, raw: str) -> MediaFileMetadata | None:
        """The metadata of the media file ``raw`` names, or ``None`` when it names no media file.

        A relative path is taken from the base directory. A file that is
        not there is still described, by its name, path and type.
        """
        path = Path(raw).expanduser()
        ext = path.suffix.lower()
        spec = self._taxonomy.classify(ext)
        if spec is None or spec.kind is MediaKind.document:
            return None
        if self._base_dir is not None:
            # Joining an absolute path leaves it as it is.
            path = self._base_dir / path
        path = path.resolve()
        try:
            size: int | None = path.stat().st_size if path.is_file() else None
        except OSError:
            size = None
        pixels = (
            ImageDimensions.read(path)
            if spec.kind is MediaKind.image and size is not None
            else None
        )
        return MediaFileMetadata(
            name=path.name,
            path=str(path),
            kind=spec.kind,
            media_type=self.MEDIA_TYPES.get(ext, spec.kind.value),
            size_bytes=size,
            pixels=pixels,
        )

    def read_all(self, raws: Iterable[str]) -> list[MediaFileMetadata]:
        """The metadata of every media file named, once each, in the order first named."""
        seen: dict[str, MediaFileMetadata] = {}
        for raw in raws:
            metadata = self.read(raw)
            if metadata is not None:
                seen.setdefault(metadata.path, metadata)
        return list(seen.values())
