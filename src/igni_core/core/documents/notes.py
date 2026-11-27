"""Every line igni writes around a document's text, word for word."""

from __future__ import annotations

import json


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

    @staticmethod
    def read_on(note: str, part: str) -> str:
        """A truncation note that names the ``part`` to read next."""
        return f"{note[:-1]} To read on, call read_document with part={_quoted(part)}.]"

    @staticmethod
    def bad_part(path: str, part: str) -> str:
        """A ``part`` that names no range."""
        return (
            f"[Could not read {path}: part={_quoted(part)} is not a range such as "
            f'"21-40", "21-" or "21", or "Sheet name!501-" for an Excel sheet.]'
        )

    @staticmethod
    def past_end(path: str, part: str, count: str) -> str:
        """A ``part`` that starts after the document's end."""
        return f"[Could not read {path}: part={_quoted(part)} is past its end; it has {count}.]"

    @staticmethod
    def no_sheet(path: str, sheet: str) -> str:
        """A ``part`` naming a sheet the workbook does not have."""
        return f"[Could not read {path}: it has no sheet named {_quoted(sheet)}.]"

    @staticmethod
    def plain_text(path: str) -> str:
        """A plain-text file, which the shell reads."""
        return f"[{path} is plain text: read it with the shell.]"

    @staticmethod
    def unsupported(path: str) -> str:
        """A binary file in a format no reader claims."""
        return (
            f"[Could not read {path}: read_document reads PDF, Word (.docx), "
            "Excel (.xlsx, .xlsm), PowerPoint (.pptx), CSV and TSV files.]"
        )

    @staticmethod
    def missing(path: str) -> str:
        """A path with no file."""
        return f"[Could not read {path}: there is no such file.]"

    @staticmethod
    def directory(path: str) -> str:
        """A path that is a directory."""
        return f"[Could not read {path}: it is a directory.]"

    @staticmethod
    def outside_project(path: str) -> str:
        """A path outside the project."""
        return f"[Could not read {path}: it is outside the project. Ask the user to attach it.]"

    @staticmethod
    def protected(path: str) -> str:
        """A key, a credential store or igni's own settings."""
        return f"[Could not read {path}: it holds credentials, keys or igni's own settings.]"


def _quoted(text: str) -> str:
    """``text`` in double quotes, as JSON writes it."""
    return json.dumps(text, ensure_ascii=False)
