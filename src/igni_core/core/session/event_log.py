"""Append-only event log coordinator for :class:`Session`.

Owns the session's ``event_log``, its ``seq`` counter, ``append`` and
``restore``.

Owns the "seq monotonic within a session" invariant: every append
increments the counter before the event is built, so persisted
rows can be reordered by ``seq`` without wall-clock skew concerns.

Persistence writes go through :class:`SessionPersistence` — the
coordinator receives a callable so it stays testable without a
DB (unit tests can inject a no-op persister).

Rule 6 (oop_offender #3): a coordinator class replaces the four
sprawled attributes / methods on the Session god-class.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from igni_core.core.session.event_log_schema import (
    SessionEvent,
    SessionEventAppendResult,
    SessionEventDraft,
)

if TYPE_CHECKING:
    from igni_core.core.session.persistence.facade import SessionPersistence

logger = logging.getLogger(__name__)


class SessionEventLog:
    """Owns the per-session append-only event log.

    Constructor takes a ``persist_ref`` callable that returns the
    current :class:`SessionPersistence` (may be ``None`` for
    bare-Session test stubs). Every :meth:`append` reads the
    current persister so a re-bind of ``session.persistence`` is
    honoured.
    """

    def __init__(
        self,
        persist_ref: Callable[[], SessionPersistence | None] | None = None,
    ) -> None:
        self._events: list[SessionEvent] = []
        self._seq: int = 0
        self._persist_ref: Callable[[], SessionPersistence | None] = persist_ref or (lambda: None)

    async def append(self, draft: SessionEventDraft) -> SessionEventAppendResult:
        """Record ``draft`` on the log and persist it.

        The counter is incremented BEFORE the event is built so a
        concurrent read of :attr:`events` mid-append never sees a
        row with ``seq=0``.

        The in-memory append cannot fail; the persistence write can.
        A failed write is reported, not raised: the in-memory log
        still reaches live clients, and the caller decides whether a
        lost persist matters.
        """
        self._seq += 1
        event = draft.to_event(self._seq)
        self._events.append(event)
        persistence = self._persist_ref()
        if persistence is None:
            return SessionEventAppendResult(persisted=False)
        try:
            # Pass the typed list directly — the persistence
            # facade dumps at its own SQL boundary (Rule 1: the
            # domain stays typed to the boundary, wire coercion
            # happens once, inside the store).
            saved = await persistence.save_event_log(list(self._events))
        except Exception as exc:  # noqa: BLE001 — a failed write is reported, not raised
            logger.warning("event_log persist failed: %s", exc)
            return SessionEventAppendResult(persisted=False, error=str(exc))
        if not saved.ok:
            logger.warning("event_log persist failed: %s", saved.error)
            return SessionEventAppendResult(persisted=False, error=saved.error)
        return SessionEventAppendResult(persisted=True)

    def restore(self, events: list[SessionEvent]) -> None:
        """Replace the in-memory log AND its seq counter atomically.

        Wired by :meth:`RehydrateController.event_log` on startup
        so the counter stays in sync with the highest ``seq`` on
        disk — otherwise the next :meth:`append` would collide
        with a persisted row.
        """
        self._events = list(events)
        self._seq = max((e.seq for e in events), default=0)

    @property
    def events(self) -> list[SessionEvent]:
        """The in-memory event log, oldest first.

        A snapshot, not a live reference: seeding rows goes through
        :meth:`restore`, which keeps the ``seq`` counter in step
        with them. A caller that got the live list could write to it
        behind the log's back and desynchronise the counter.
        """
        return list(self._events)

    @property
    def seq(self) -> int:
        """Current monotonic sequence counter."""
        return self._seq
