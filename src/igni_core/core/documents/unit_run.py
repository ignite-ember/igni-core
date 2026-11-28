"""The units of a document a window lets through, read until the budget runs out."""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, Field

from igni_core.core.documents.budget import ReadBudget
from igni_core.core.documents.window import ReadWindow


class UnitRun(BaseModel):
    """Each read unit's text from the window's first, and the unit a cap stopped in."""

    first: int = 1
    texts: list[str] = Field(default_factory=list)
    stopped: int = 0
    """The unit the character or time cap stopped in; 0 when it did not."""

    @classmethod
    def collect(
        cls,
        total: int,
        render: Callable[[int], str],
        budget: ReadBudget,
        window: ReadWindow,
        cap: int | None = None,
    ) -> UnitRun:
        """Units ``window.first`` on, each as ``render(n)`` gives it, up to ``cap`` of them."""
        run = cls(first=window.first)
        for n in range(window.first, window.end(cap or total, total) + 1):
            text = render(n)
            if budget.late and not text:
                run.stopped = n
                break
            run.texts.append(text)
            if budget.spent:
                run.stopped = n
                break
        return run

    def numbered(self) -> list[tuple[int, str]]:
        """Each unit's number with its text."""
        return list(enumerate(self.texts, start=self.first))
