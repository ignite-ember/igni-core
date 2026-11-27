"""The legacy Office formats igni does not read: a note that says so, and what to save as."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from igni_core.core.documents.budget import ReadBudget
from igni_core.core.documents.exceptions import DocumentRefused
from igni_core.core.documents.notes import DocumentNotes
from igni_core.core.documents.reader import DocumentReader
from igni_core.core.documents.schemas import DocumentText


class LegacyDocumentReader(DocumentReader):
    """``.xls``, ``.doc`` and ``.ppt``: never opened."""

    _MODERN: ClassVar[dict[str, str]] = {".xls": ".xlsx", ".doc": ".docx", ".ppt": ".pptx"}
    extensions: ClassVar[frozenset[str]] = frozenset(_MODERN)

    def _read(self, path: Path, budget: ReadBudget) -> DocumentText:
        suffix = path.suffix.lower()
        raise DocumentRefused(DocumentNotes.legacy(str(path), suffix, self._MODERN[suffix]))
