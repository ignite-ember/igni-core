"""The legacy Office formats igni does not read: a note that says so, what to save as, and to propose a reader."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from igni_core.core.documents.budget import ReadBudget
from igni_core.core.documents.exceptions import DocumentRefused
from igni_core.core.documents.notes import DocumentNotes
from igni_core.core.documents.reader import DocumentReader
from igni_core.core.documents.schemas import DocumentText
from igni_core.core.documents.window import ReadWindow


class LegacyDocumentReader(DocumentReader):
    """``.doc`` and ``.ppt``: never opened, as reading them needs programs outside igni."""

    _MODERN: ClassVar[dict[str, str]] = {".doc": ".docx", ".ppt": ".pptx"}
    extensions: ClassVar[frozenset[str]] = frozenset(_MODERN)

    def _read(self, path: Path, budget: ReadBudget, window: ReadWindow) -> DocumentText:
        suffix = path.suffix.lower()
        raise DocumentRefused(DocumentNotes.legacy(str(path), suffix, self._MODERN[suffix]))
