"""The checks an Office Open XML file passes before a parser opens it."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import ClassVar

from igni_core.core.documents.exceptions import DocumentRefused
from igni_core.core.documents.notes import DocumentNotes


class OoxmlArchive:
    """Refuses an encrypted file and a zip that unpacks too large."""

    _CFB_MAGIC: ClassVar[bytes] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    _ENCRYPTION_INFO: ClassVar[bytes] = "EncryptionInfo".encode("utf-16-le")

    @classmethod
    def check(cls, path: Path, max_unzipped: int) -> None:
        """Raise :class:`DocumentRefused` for an encrypted or oversized file, ``ValueError`` for a non-zip."""
        with path.open("rb") as handle:
            head = handle.read(len(cls._CFB_MAGIC))
        if head == cls._CFB_MAGIC:
            if cls._ENCRYPTION_INFO in path.read_bytes():
                raise DocumentRefused(DocumentNotes.password_protected(str(path)))
            raise ValueError("an OLE compound file, not an Office Open XML package")
        with zipfile.ZipFile(path) as archive:
            if sum(info.file_size for info in archive.infolist()) > max_unzipped:
                raise DocumentRefused(DocumentNotes.too_large(str(path)))

    @staticmethod
    def member(archive: zipfile.ZipFile, name: str, path: Path, limit: int) -> bytes:
        """The member's bytes, never more than ``limit`` whatever its header claims; ``KeyError`` when absent."""
        with archive.open(name) as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise DocumentRefused(DocumentNotes.too_large(str(path)))
        return data
