"""What the card actually drew, told back to the model.

The visualizer's spec is one-way: it goes to the client on the stream
and never comes back into the model's context. So the only thing the
model could say about a card was what it hoped for — "Rendered a
6-element chart" over a card that drew nothing (R666).

This module is the return path. The client renders the card and says
what it drew of it; the backend keeps that verdict against the card's
spec id and revision; the visualizer's recap quotes it. What the model
says about the card is then what the renderer saw, rather than what
the model expected to see.

Three pieces:

- :class:`VisualizationVerdict` — the verdict, and the words it is
  said in.
- :class:`VerdictBook` — the store: the verdicts, and the waiters for
  them.
- :data:`VERDICTS` — the book's instance. One per process: a card's
  spec id is minted per visualizer run, so a verdict names one card
  and cannot answer for another.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import OrderedDict

from pydantic import BaseModel, ConfigDict, Field

#: How long a recap waits for the client's verdict, in seconds. The
#: client answers in milliseconds — it answers while the visualizer is
#: still writing its own summary. The bound is for the runs where
#: nobody will answer at all: a headless run with no window on it.
DEFAULT_WAIT_S = 1.5

#: The environment variable that sets the wait.
WAIT_ENV = "IGNI_VISUALIZATION_VERDICT_WAIT_S"


def verdict_wait_seconds() -> float:
    """How long a recap waits for the client's verdict.

    Read from the environment on every call rather than once at
    import, so a run can shorten the wait without a code change.
    """
    raw = os.environ.get(WAIT_ENV, "").strip()
    if not raw:
        return DEFAULT_WAIT_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_WAIT_S
    return value if value >= 0.0 else DEFAULT_WAIT_S


class ElementFailure(BaseModel):
    """One element of the card the client could not draw."""

    model_config = ConfigDict(frozen=True)

    type: str = Field(default="", description="The element's type, e.g. ``BarGraph``.")
    message: str = Field(
        default="", description="Why the client could not draw it, in the client's words."
    )


class VisualizationVerdict(BaseModel):
    """What the client drew of one card.

    ``total`` is how many elements the card asked for, ``failed`` the
    ones the client could not draw, and ``issues`` what was wrong with
    the card's shape — a child that is not an element of it, an
    element nothing refers to.
    """

    model_config = ConfigDict(frozen=True)

    spec_id: str = Field(description="The card this verdict is about.")
    revision: int = Field(default=0, description="Which draw of the card this is.")
    total: int = Field(default=0, description="How many elements the card has.")
    failed: tuple[ElementFailure, ...] = Field(
        default=(), description="The elements the client could not draw."
    )
    issues: tuple[str, ...] = Field(default=(), description="What was wrong with the card's shape.")

    @property
    def drawn(self) -> int:
        """How many elements the client drew."""
        return max(0, self.total - len(self.failed))

    def said(self) -> str:
        """The verdict in the words the model reads.

        This is model-facing text: it goes into the visualizer's recap
        and from there into the agent that asked for the card. It says
        what the card shows and nothing else — no "rendered", no
        "ready", no word for a state the client did not report.
        """
        if self.total <= 0:
            head = "The card drew nothing."
        elif self.drawn >= self.total:
            head = f"The card drew all {self.total} elements."
        else:
            head = f"The card drew {self.drawn} of {self.total} elements."
        parts = [head]
        for failure in self.failed:
            detail = failure.message.strip()
            parts.append(
                f"It could not draw {failure.type}: {detail}"
                if detail
                else f"It could not draw {failure.type}."
            )
        for issue in self.issues:
            parts.append(f"It could not be fully drawn: {issue}")
        return " ".join(parts)


class VerdictBook:
    """The book's verdicts, and the waiters for them.

    Keyed by (spec id, revision): a card is drawn, redrawn and drawn
    again over one stream, and each of those is its own revision. The
    revision is what keeps a late report about an earlier draw from
    answering for a later one.
    """

    def __init__(self, max_entries: int = 64) -> None:
        self._verdicts: OrderedDict[tuple[str, int], VisualizationVerdict] = OrderedDict()
        self._waiters: dict[tuple[str, int], asyncio.Event] = {}
        self._max_entries = max_entries

    def report(self, verdict: VisualizationVerdict) -> None:
        """Record what a client drew, and wake whoever waits for it.

        The oldest verdict is dropped once the book is full: a card
        that nobody is waiting for any more is worth nothing, and the
        book is per process.
        """
        key = (verdict.spec_id, verdict.revision)
        self._verdicts[key] = verdict
        self._verdicts.move_to_end(key)
        while len(self._verdicts) > self._max_entries:
            self._verdicts.popitem(last=False)
        waiter = self._waiters.get(key)
        if waiter is not None:
            waiter.set()

    def get(self, spec_id: str, revision: int) -> VisualizationVerdict | None:
        """The verdict for one draw of a card, when it has been reported."""
        return self._verdicts.get((spec_id, revision))

    async def await_verdict(
        self, spec_id: str, revision: int, wait_s: float | None = None
    ) -> VisualizationVerdict | None:
        """Wait for the verdict of one draw of a card.

        Returns the verdict when it was reported before the wait
        started or during it, and ``None`` when nobody answers within
        the wait. The wait is bounded: a run must not hang on a client
        that will never answer.
        """
        key = (spec_id, revision)
        budget = verdict_wait_seconds() if wait_s is None else wait_s
        deadline = time.monotonic() + budget
        while True:
            verdict = self._verdicts.get(key)
            if verdict is not None:
                return verdict
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return None
            waiter = asyncio.Event()
            self._waiters[key] = waiter
            try:
                await asyncio.wait_for(waiter.wait(), timeout=remaining)
            except TimeoutError:
                # A report that landed between the lookup above and the
                # wait is in the book even though it could not set the
                # waiter: read it before giving up on it.
                return self._verdicts.get(key)
            finally:
                if self._waiters.get(key) is waiter:
                    del self._waiters[key]


#: The book's verdicts. One per process: a card's spec id is minted
#: per visualizer run, so a verdict names one card and cannot answer
#: for another.
VERDICTS = VerdictBook()


__all__ = [
    "DEFAULT_WAIT_S",
    "VERDICTS",
    "WAIT_ENV",
    "ElementFailure",
    "VerdictBook",
    "VisualizationVerdict",
    "verdict_wait_seconds",
]
