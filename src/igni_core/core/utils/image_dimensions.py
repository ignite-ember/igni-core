"""An image's pixel size, read from its header with the standard library alone."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import BinaryIO, ClassVar

from igni_core.core.utils.media_schemas import PixelSize


class ImageDimensions:
    """Reads the width and height of a PNG, GIF, JPEG or WebP file from its header bytes."""

    #: How many leading bytes the PNG, GIF and WebP headers need.
    _HEAD_BYTES: ClassVar[int] = 32
    #: JPEG start-of-frame markers, which carry the size (DHT, JPG and DAC excluded).
    _JPEG_SOF: ClassVar[frozenset[int]] = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
    #: JPEG markers with no length field after them: TEM and the restart markers.
    _JPEG_BARE: ClassVar[frozenset[int]] = frozenset({0x01, *range(0xD0, 0xD8)})
    #: How many JPEG segments are walked before giving up.
    _JPEG_MAX_SEGMENTS: ClassVar[int] = 512

    @classmethod
    def read(cls, path: Path) -> PixelSize | None:
        """The image's pixel size, or ``None`` for another format or an unreadable header."""
        try:
            with path.open("rb") as handle:
                head = handle.read(cls._HEAD_BYTES)
                size = cls._from_head(head)
                if size is None and head[:2] == b"\xff\xd8":
                    handle.seek(2)
                    size = cls._jpeg(handle)
        except (OSError, struct.error):
            return None
        if size is None or size.width <= 0 or size.height <= 0:
            return None
        return size

    @classmethod
    def _from_head(cls, head: bytes) -> PixelSize | None:
        """The size from the fixed-offset headers: PNG, GIF and WebP."""
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            width, height = struct.unpack(">II", head[16:24])
            return PixelSize(width=width, height=height)
        if head[:6] in (b"GIF87a", b"GIF89a"):
            width, height = struct.unpack("<HH", head[6:10])
            return PixelSize(width=width, height=height)
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return cls._webp(head)
        return None

    @staticmethod
    def _webp(head: bytes) -> PixelSize | None:
        """The size from a WebP file's first chunk: lossy, lossless or extended."""
        chunk = head[12:16]
        if chunk == b"VP8 " and head[23:26] == b"\x9d\x01\x2a":
            width, height = struct.unpack("<HH", head[26:30])
            return PixelSize(width=width & 0x3FFF, height=height & 0x3FFF)
        if chunk == b"VP8L" and head[20:21] == b"\x2f":
            bits = int.from_bytes(head[21:25], "little")
            return PixelSize(width=(bits & 0x3FFF) + 1, height=((bits >> 14) & 0x3FFF) + 1)
        if chunk == b"VP8X":
            width = int.from_bytes(head[24:27], "little") + 1
            height = int.from_bytes(head[27:30], "little") + 1
            return PixelSize(width=width, height=height)
        return None

    @classmethod
    def _jpeg(cls, handle: BinaryIO) -> PixelSize | None:
        """Walk the JPEG segments to the first start-of-frame and read its size."""
        for _ in range(cls._JPEG_MAX_SEGMENTS):
            byte = handle.read(1)
            if byte != b"\xff":
                return None
            marker = handle.read(1)
            while marker == b"\xff":
                marker = handle.read(1)
            if not marker:
                return None
            code = marker[0]
            if code in cls._JPEG_BARE:
                continue
            if code in (0xD9, 0xDA):
                return None
            (length,) = struct.unpack(">H", handle.read(2))
            if code in cls._JPEG_SOF:
                _precision, height, width = struct.unpack(">BHH", handle.read(5))
                return PixelSize(width=width, height=height)
            # A length below two steps back onto its own zero bytes, which end the walk.
            handle.seek(length - 2, 1)
        return None
