"""One sheet of a workbook, whatever format it came from: its name, and its rows on demand."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence


class WorkbookSheet:
    """A sheet's title, and a callable that yields its rows as cell texts."""

    def __init__(self, title: str, rows: Callable[[], Iterable[Sequence[str]]]) -> None:
        self.title = title
        self._rows = rows

    def rows(self) -> Iterable[Sequence[str]]:
        """The sheet's rows, read only now."""
        return self._rows()
