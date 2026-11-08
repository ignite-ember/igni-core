"""Typed schema for the per-session append-only event log.

Every log entry is a Pydantic
model with explicit fields, not a free-form ``dict[str, Any]``.

Wire format is still ``dict`` — persistence.py stores the log in
Agno's session ``session_data.event_log`` column which handles
JSON round-trip. Callers dump with ``model_dump()`` at the
storage boundary and parse back with :meth:`SessionEvent.from_wire`
on load. Every construction site goes through :class:`SessionEvent`,
so shape drift fails loud at validation time rather than
surfacing as a `KeyError` deep in the splicer.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SessionEventDraft(BaseModel):
    """An event on its way into the log: everything but the ``seq`` and
    timestamp the log stamps on it.

    Producers hand the log one of these instead of a ``(type, payload)``
    pair. A producer whose payload is a model builds the draft with
    :meth:`of`, so the dump happens in one place.
    """

    model_config = ConfigDict(extra="forbid")

    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    run_id: str = ""

    @field_validator("run_id", mode="before")
    @classmethod
    def _run_id_as_text(cls, value: object) -> str:
        return "" if value is None else str(value)

    @classmethod
    def of(cls, event_type: str, payload: BaseModel, run_id: str = "") -> SessionEventDraft:
        """A draft whose payload is ``payload`` dumped."""
        return cls(type=event_type, payload=payload.model_dump(), run_id=run_id)

    def stamped(self, seq: int) -> SessionEvent:
        """The logged event, at ``seq``.

        The payload is copied, so a producer mutating its dict after the
        append does not change the stored entry.
        """
        return SessionEvent(
            seq=seq,
            run_id=self.run_id,
            type=self.type,
            payload=dict(self.payload),
        )


class SessionEvent(BaseModel):
    """One entry in ``Session.event_log``.

    - ``seq`` — monotonic per-session counter. FE replay relies on
      this (not ``timestamp_ms``) because wall-clock is subject to
      skew and same-millisecond collisions.
    - ``run_id`` — the run that emitted the event. Empty string
      for boot-level events with no owning run.
    - ``timestamp_ms`` — Unix epoch milliseconds; only for FE
      display, never for ordering.
    - ``type`` — event kind (``"visualization_delta"``,
      ``"content_preview"``, ``"orchestrate_event"``, …). Kept as
      ``str`` rather than ``Literal`` because the set is
      producer-open — new event kinds land here without a schema
      change.
    - ``payload`` — event-kind-specific dict. Individual splicers
      cast it to their own typed model at read time.
    """

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    run_id: str = ""
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_wire(cls, entry: dict[str, Any]) -> SessionEvent | None:
        """Parse one persisted entry back to a :class:`SessionEvent`.

        Returns ``None`` on any validation failure — a stale/corrupt
        row shouldn't sink the whole log load. Callers filter
        ``None``s from the result list.
        """
        try:
            return cls.model_validate(entry)
        except Exception:
            return None
