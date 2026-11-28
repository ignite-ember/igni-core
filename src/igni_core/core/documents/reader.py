"""The base every document reader shares: its caps, and a failure that never escapes."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

from igni_core.core.documents.budget import ReadBudget
from igni_core.core.documents.exceptions import DocumentRefused
from igni_core.core.documents.notes import DocumentNotes
from igni_core.core.documents.schemas import DocumentText
from igni_core.core.documents.window import ReadWindow

logger = logging.getLogger(__name__)


class DocumentReader(ABC):
    """Reads the files of one format as text, within the caps below."""

    extensions: ClassVar[frozenset[str]]
    MAX_BYTES: ClassVar[int] = 100 * 1024 * 1024
    """A larger file is not opened."""
    MAX_UNZIPPED_BYTES: ClassVar[int] = 300 * 1024 * 1024
    """An Office file that unpacks to more is not opened."""
    MAX_CHARS: ClassVar[int] = 100_000
    """About 25k tokens; a longer document is cut, and says so."""
    MAX_SECONDS: ClassVar[int] = 15
    """Reading stops here, and says so."""
    MAX_ROWS: ClassVar[int] = 500
    """Rows per sheet or table."""
    MAX_SHEETS: ClassVar[int] = 20
    MAX_SLIDES: ClassVar[int] = 100
    NAMED_SHEETS: ClassVar[bool] = False
    """Whether a window may name a sheet."""

    def read(self, path: Path, window: ReadWindow | None = None) -> DocumentText:
        """The document's text in ``window``, or a note; never an exception."""
        window = window or ReadWindow()
        try:
            if path.stat().st_size > self.MAX_BYTES:
                raise DocumentRefused(DocumentNotes.too_large(str(path)))
            if window.sheet is not None and not self.NAMED_SHEETS:
                raise DocumentRefused(DocumentNotes.bad_part(str(path), window.part))
            budget = ReadBudget.start(self.MAX_CHARS, self.MAX_SECONDS)
            return self._read(path, budget, window)
        except DocumentRefused as refused:
            return DocumentText(path=str(path), note=refused.note)
        except Exception as exc:  # noqa: BLE001 — a file that will not parse is reported, not raised
            logger.info("could not read the text of %s: %s", path, exc)
            return DocumentText(path=str(path))

    @abstractmethod
    def _read(self, path: Path, budget: ReadBudget, window: ReadWindow) -> DocumentText:
        """The text of a file in ``window`` within ``budget``; may raise."""
