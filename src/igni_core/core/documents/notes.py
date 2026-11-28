"""Every line igni writes around a document's text, word for word."""

from __future__ import annotations


class DocumentNotes:
    """The model-facing framing, truncation and refusal notes."""

    @staticmethod
    def extracted(path: str, count: str) -> str:
        """The line above a document's text."""
        if not count:
            return f"[Text extracted from {path}:]"
        return f"[Text extracted from {path} ({count}):]"

    @staticmethod
    def no_text(path: str) -> str:
        """A document that gave no text: empty, scanned, damaged or not what its name says."""
        return f"[No text could be extracted from {path}.]"

    @staticmethod
    def chars_cut(limit: int) -> str:
        """The text stopped at the character cap."""
        return f"[Text truncated at {limit} characters.]"

    @staticmethod
    def time_cut(seconds: int) -> str:
        """The text stopped at the time cap."""
        return f"[Text truncated after {seconds} seconds of reading.]"

    @staticmethod
    def parts_cut(unit: str, shown: int, total: int) -> str:
        """Only the first ``shown`` of ``total`` rows, sheets or slides are given."""
        return f"[{unit} truncated at {shown} of {total}.]"

    @staticmethod
    def password_protected(path: str) -> str:
        """An encrypted document."""
        return f"[Could not read {path}: it is password-protected.]"

    @staticmethod
    def too_large(path: str) -> str:
        """A document over the size cap."""
        return f"[Could not read {path}: it is too large to read.]"

    @staticmethod
    def legacy(path: str, suffix: str, modern: str) -> str:
        """A legacy Office format igni does not read."""
        return f"[Could not read {path}: the legacy {suffix} format is not supported. Save it as {modern}.]"

    @staticmethod
    def counted(n: int, unit: str) -> str:
        """``1 page``, ``2 pages``."""
        return f"{n} {unit}" + ("" if n == 1 else "s")
