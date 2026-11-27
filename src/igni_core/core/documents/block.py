"""The lines that carry one document's text to the model, attached or read with ``read_document``."""

from __future__ import annotations

import re
from typing import ClassVar

from igni_core.core.documents.notes import DocumentNotes
from igni_core.core.documents.schemas import DocumentText
from igni_core.core.hooks.untrusted import wrap_untrusted


class DocumentBlock:
    """A document's note, or the line naming it above its quarantined text."""

    _WRAPPER_TAG: ClassVar[re.Pattern[str]] = re.compile(
        r"<(?=\s*/?\s*(?:system-context|attached-files|extension-message|loop-iteration)\b)",
        re.IGNORECASE,
    )
    """The ``<`` opening one of igni's wrapper tags, written ``&lt;`` inside a document."""

    @classmethod
    def lines(cls, document: DocumentText) -> str:
        """The text the model reads for ``document``."""
        if document.note:
            return document.note
        if not document.has_text:
            return DocumentNotes.no_text(document.path)
        body = "\n".join([cls._WRAPPER_TAG.sub("&lt;", document.body), *document.notes])
        head = DocumentNotes.extracted(document.path, document.count)
        return f"{head}\n{wrap_untrusted(body)}"
