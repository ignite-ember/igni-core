"""An Apple Numbers, Pages or Keynote file, through the preview PDF it may carry."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import ClassVar

from igni_core.core.documents.budget import ReadBudget
from igni_core.core.documents.exceptions import DocumentRefused
from igni_core.core.documents.notes import DocumentNotes
from igni_core.core.documents.ooxml_archive import OoxmlArchive
from igni_core.core.documents.pdf_reader import PdfDocumentReader
from igni_core.core.documents.reader import DocumentReader
from igni_core.core.documents.schemas import DocumentText
from igni_core.core.documents.window import ReadWindow


class IworkDocumentReader(DocumentReader):
    """A file or a folder bundle: its preview PDF read as a PDF and said to be a preview; without one, a note to export it."""

    _APPS: ClassVar[dict[str, tuple[str, str]]] = {
        ".numbers": ("Numbers", ".xlsx or .csv"),
        ".pages": ("Pages", ".docx or PDF"),
        ".key": ("Keynote", ".pptx or PDF"),
    }
    extensions: ClassVar[frozenset[str]] = frozenset(_APPS)
    BUNDLES: ClassVar[bool] = True
    _PREVIEWS: ClassVar[tuple[str, ...]] = ("quicklook/preview.pdf", "preview.pdf")
    """Where iWork saves a preview PDF, compared lower case; current versions save only images."""

    def __init__(self, pdf: PdfDocumentReader | None = None) -> None:
        self._pdf = pdf or PdfDocumentReader()

    def _read(self, path: Path, budget: ReadBudget, window: ReadWindow) -> DocumentText:
        app, exports = self._APPS[path.suffix.lower()]
        preview = self._bundle_preview(path) if path.is_dir() else self._zip_preview(path)
        if preview is None:
            raise DocumentRefused(DocumentNotes.iwork_no_preview(str(path), app, exports))
        document = self._pdf.pages(io.BytesIO(preview), str(path), budget, window)
        note = DocumentNotes.iwork_preview(app, exports)
        return document.model_copy(update={"notes": [note, *document.notes]})

    def _zip_preview(self, path: Path) -> bytes | None:
        OoxmlArchive.check(path, self.MAX_UNZIPPED_BYTES)
        with zipfile.ZipFile(path) as archive:
            names = {name.lower(): name for name in archive.namelist()}
            for wanted in self._PREVIEWS:
                if wanted in names:
                    return OoxmlArchive.member(archive, names[wanted], path, self.MAX_BYTES)
        return None

    def _bundle_preview(self, path: Path) -> bytes | None:
        """The bundle's preview PDF, only if it is a file inside the bundle once links are followed."""
        root = path.resolve()
        for wanted in self._PREVIEWS:
            found = self._find(root, wanted.split("/"))
            if found is None:
                continue
            target = found.resolve()
            if not target.is_relative_to(root) or not target.is_file():
                continue
            if target.stat().st_size > self.MAX_BYTES:
                raise DocumentRefused(DocumentNotes.too_large(str(path)))
            return target.read_bytes()
        return None

    @staticmethod
    def _find(root: Path, parts: list[str]) -> Path | None:
        """The path under ``root`` whose parts match ``parts`` ignoring case."""
        current = root
        for part in parts:
            matches = [child for child in current.iterdir() if child.name.lower() == part]
            if not matches:
                return None
            current = matches[0]
            if part != parts[-1] and not current.is_dir():
                return None
        return current
