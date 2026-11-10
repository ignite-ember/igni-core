"""Event-log store — persists the per-session append-only log.

Composes :class:`SessionDataService`. Load parses each persisted
row through :data:`SESSION_EVENT_ADAPTER` and drops the rows that
don't validate — stale, or written by a build that modelled a kind
this one doesn't — so they don't surface as a ``KeyError`` deep in
the splicer. Save dumps at the SQL boundary.

The wire semantics match the pre-refactor
``SessionPersistence.load_event_log`` / ``save_event_log`` pair:
the persisted shape stays ``list[dict]`` (Agno's ``session_data``
column round-trips JSON without a Pydantic step), and every
returned entry rolls through one :class:`SessionEvent` definition.
A row's payload is normalised on the way in — the card's own
fields always land — so a row written by an earlier build reads
back with the keys the log writes today.
"""

from __future__ import annotations

import logging

from igni_core.core.session.event_log_schema import (
    SESSION_EVENT_ADAPTER,
    SessionEvent,
)
from igni_core.core.session.persistence.data_service import SessionDataService
from igni_core.core.session.schemas import LoadResult, PersistResult

logger = logging.getLogger(__name__)


class EventLogStore:
    """Coordinator for the ``event_log`` sub-blob of ``session_data``."""

    KEY = "event_log"

    def __init__(self, data_service: SessionDataService) -> None:
        self._data = data_service

    async def load(self) -> LoadResult[list[SessionEvent]]:
        """Read the persisted event log as typed :class:`SessionEvent`
        rows.

        Every persisted entry is round-tripped through
        :data:`SESSION_EVENT_ADAPTER` before being surfaced —
        stale / schema-drifted rows drop, and the output list comes
        from one Pydantic definition (Rule 1).
        """
        return await self._data.read_key(self.KEY, self._parse)

    async def save(self, events: list[SessionEvent]) -> PersistResult:
        """Atomic-replace the persisted event log.

        Callers pass typed :class:`SessionEvent` instances; the
        store dumps each to a wire dict at the SQL boundary so the
        domain stays typed and the wire-dump happens exactly
        once, inside the store (Rule 1).
        """
        wire = [e.model_dump() for e in events]
        return await self._data.write_key(self.KEY, wire)

    @staticmethod
    def _parse(raw: object) -> list[SessionEvent]:
        """Coerce a persisted ``list[dict]`` back to
        ``list[SessionEvent]``. Malformed entries drop."""
        if not isinstance(raw, list):
            return []
        out: list[SessionEvent] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            try:
                out.append(SESSION_EVENT_ADAPTER.validate_python(entry))
            except Exception as exc:  # noqa: BLE001 — a stale row must not sink the load
                logger.warning("dropping unreadable session event row: %s", exc)
        return out
