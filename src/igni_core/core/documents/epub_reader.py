"""An EPUB book, chapter by chapter in its reading order."""

from __future__ import annotations

import posixpath
import zipfile
from pathlib import Path
from typing import ClassVar
from urllib.parse import unquote

from igni_core.core.documents.budget import ReadBudget
from igni_core.core.documents.html_blocks import HtmlBlocks
from igni_core.core.documents.notes import DocumentNotes
from igni_core.core.documents.ooxml_archive import OoxmlArchive
from igni_core.core.documents.reader import DocumentReader
from igni_core.core.documents.safe_xml import SafeXml
from igni_core.core.documents.schemas import DocumentText
from igni_core.core.documents.unit_run import UnitRun
from igni_core.core.documents.window import ReadWindow


class EpubDocumentReader(DocumentReader):
    """Each spine chapter under ``--- Chapter n ---``, headings kept; scripts, styles and DRM-encrypted chapters dropped."""

    extensions: ClassVar[frozenset[str]] = frozenset({".epub"})
    _CONTAINER: ClassVar[str] = "META-INF/container.xml"
    _ENCRYPTION: ClassVar[str] = "META-INF/encryption.xml"

    def _read(self, path: Path, budget: ReadBudget, window: ReadWindow) -> DocumentText:
        OoxmlArchive.check(path, self.MAX_UNZIPPED_BYTES)
        with zipfile.ZipFile(path) as archive:
            chapters = self._spine(archive, path)
            count = DocumentNotes.counted(len(chapters), "chapter")
            if window.past(len(chapters)):
                raise window.past_end(str(path), count)
            run = UnitRun.collect(
                len(chapters),
                lambda n: self._chapter(archive, path, chapters[n - 1], budget),
                budget,
                window,
            )
        return DocumentText(
            path=str(path),
            count=count,
            body="\n".join(
                f"--- Chapter {n} ---" + (f"\n{text}" if text else "") for n, text in run.numbered()
            ),
            has_text=any(run.texts),
            notes=budget.notes(window.continuation(budget, run.stopped)),
        )

    def _spine(self, archive: zipfile.ZipFile, path: Path) -> list[str]:
        """The chapters' member names in reading order, encrypted ones left out."""
        container = SafeXml.xml(self._member(archive, self._CONTAINER, path))
        rootfile = next(e for e in container.iter() if SafeXml.name(e) == "rootfile")
        opf_name = rootfile.get("full-path") or ""
        opf = SafeXml.xml(self._member(archive, opf_name, path))
        base = posixpath.dirname(opf_name)
        hrefs = {
            item.get("id"): item.get("href") or ""
            for item in opf.iter()
            if SafeXml.name(item) == "item"
        }
        names = [
            posixpath.normpath(posixpath.join(base, unquote(hrefs[ref.get("idref")])))
            for ref in opf.iter()
            if SafeXml.name(ref) == "itemref" and ref.get("idref") in hrefs
        ]
        encrypted = self._encrypted(archive, path)
        present = set(archive.namelist())
        return [name for name in names if name in present and name not in encrypted]

    def _encrypted(self, archive: zipfile.ZipFile, path: Path) -> set[str]:
        if self._ENCRYPTION not in archive.namelist():
            return set()
        root = SafeXml.xml(self._member(archive, self._ENCRYPTION, path))
        return {
            posixpath.normpath(unquote(ref.get("URI") or ""))
            for ref in root.iter()
            if SafeXml.name(ref) == "cipherreference"
        }

    def _chapter(self, archive: zipfile.ZipFile, path: Path, name: str, budget: ReadBudget) -> str:
        if budget.overdue():
            return ""
        try:
            blocks = HtmlBlocks.of(SafeXml.markup(self._member(archive, name, path)))
        except ValueError:
            return ""
        lines: list[str] = []
        for block in blocks:
            text = block.render(budget, self.MAX_ROWS)
            if text:
                lines.append(text)
            if budget.spent:
                break
        return "\n".join(lines)

    def _member(self, archive: zipfile.ZipFile, name: str, path: Path) -> bytes:
        return OoxmlArchive.member(archive, name, path, self.MAX_PART_BYTES)
