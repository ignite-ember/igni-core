"""The refusal a document reader raises."""

from __future__ import annotations


class DocumentRefused(Exception):
    """The document is not read, and ``note`` says why."""

    def __init__(self, note: str) -> None:
        super().__init__(note)
        self.note = note


__all__ = ["DocumentRefused"]
