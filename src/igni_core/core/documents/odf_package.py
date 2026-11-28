"""An OpenDocument file's ``content.xml``, opened behind the zip guard."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import ClassVar

from lxml import etree

from igni_core.core.documents.exceptions import DocumentRefused
from igni_core.core.documents.notes import DocumentNotes
from igni_core.core.documents.odf_text import OdfText
from igni_core.core.documents.ooxml_archive import OoxmlArchive
from igni_core.core.documents.safe_xml import SafeXml


class OdfPackage:
    """Refuses an encrypted or oversized package; macros and embedded objects are never opened."""

    _ENCRYPTED: ClassVar[bytes] = b"encryption-data"

    @classmethod
    def body(cls, path: Path, max_unzipped: int, max_part: int, kind: str) -> etree._Element:
        """The ``office:<kind>`` element of ``content.xml``: ``text``, ``spreadsheet`` or ``presentation``."""
        OoxmlArchive.check(path, max_unzipped)
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "META-INF/manifest.xml" in names:
                manifest = OoxmlArchive.member(archive, "META-INF/manifest.xml", path, max_part)
                if cls._ENCRYPTED in manifest:
                    raise DocumentRefused(DocumentNotes.password_protected(str(path)))
            content = OoxmlArchive.member(archive, "content.xml", path, max_part)
        root = SafeXml.xml(content)
        body = root.find(
            f"{OdfText.tag(OdfText.OFFICE, 'body')}/{OdfText.tag(OdfText.OFFICE, kind)}"
        )
        if body is None:
            raise ValueError(f"no office:{kind} in content.xml")
        return body
