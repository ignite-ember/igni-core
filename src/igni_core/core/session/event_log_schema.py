"""Typed schema for the per-session append-only event log.

The log holds a closed set of event kinds, each one a Pydantic model
in a ``Literal``-tagged union — never a free-form
``dict[str, Any]`` (Rule 1 / Pattern 2). Two shapes per kind:

* the **draft** — what a producer hands the log: the payload and the
  run that emitted it. The log stamps ``seq`` and ``timestamp_ms``.
* the **entry** — what the log holds and what the store persists.

The kinds are:

* ``visualization_delta`` — a viz card: the visualizer's spec, or an
  extension's ``chat.append`` block (which adds a ``title`` and the
  extension's id as ``source``).
* ``workflow_event`` — a workflow runtime record, the shape the
  frontend also receives on the ``workflow_event`` push channel.

Wire format is still ``dict`` — the store keeps the log in Agno's
session ``session_data.event_log`` column, which round-trips JSON.
The dump happens once, at the store's SQL boundary; parsing comes
back through :data:`SESSION_EVENT_ADAPTER`, so a row written by a
build that modelled a kind this one doesn't drops instead of
reaching a splicer as an untyped blob.
"""

from __future__ import annotations

import time
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_serializer,
    field_validator,
)

from igni_core.backend.schemas_workflow_events import WorkflowEventRecord
from igni_core.core.tools.orchestrate_events import VisualizationDeltaEvent

#: The kinds of event the session log holds. The set is closed: a new
#: kind is a new draft + entry class here, not a free-form string
#: smuggled through a payload.
SessionEventKind = Literal["visualization_delta", "workflow_event"]


# Subclasses the wire event the visualizer and the extensions send, so
# the log keeps one shape for a card. ``final`` defaults to True here
# (the wire event defaults to False): a payload without the key means
# "the card is complete", which the view list and the history splicer
# have always read. The card's own fields default too: a producer that
# sends a partial card is logged as sent, and readers skip what they
# can't use.
class VisualizationDeltaPayload(VisualizationDeltaEvent):
    """A visualization card recorded in the session's event log.

    The card's spec arrives as JSON text, possibly in several partial
    updates sharing one ``spec_id``; the update with ``final`` set holds
    the complete spec. ``title`` and ``source`` are set only for cards
    an extension added to the chat.
    """

    type: Literal["visualization_delta"] = Field(default="visualization_delta", frozen=True)
    agent_path: str = Field(
        default="",
        description="Dot-joined names of the agent that produced the card. Empty when not known.",
    )
    spec_id: str = Field(
        default="",
        description="Identifier of the card; later updates with the same id replace earlier ones.",
    )
    spec_json: str = Field(
        default="",
        alias="json",
        description="The card's visualization spec as JSON text. Partial (possibly incomplete "
        "JSON) until `final` is true.",
    )
    final: bool = Field(
        default=True,
        description="True when `json` holds the complete spec; false for an in-progress update.",
    )
    title: str = Field(
        default="", description="Card title given by the extension that added it. Empty otherwise."
    )
    source: str = Field(
        default="",
        description="Id of the extension that added the card. Empty for cards produced by an agent.",
    )


# ── Drafts — what a producer hands the log ────────────────────


class SessionEventDraftBase(BaseModel):
    """Common shape of an event on its way into the log.

    ``run_id`` is the run that emitted the event — empty for a
    boot-level event with no owning run.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = ""

    @field_validator("run_id", mode="before")
    @classmethod
    def _run_id_as_text(cls, value: object) -> str:
        return "" if value is None else str(value)

    def to_event(self, seq: int) -> SessionEvent:
        """The entry this draft becomes, stamped at ``seq``."""
        raise NotImplementedError


class VisualizationDeltaDraft(SessionEventDraftBase):
    """A viz card on its way into the log."""

    type: Literal["visualization_delta"] = "visualization_delta"
    payload: VisualizationDeltaPayload

    def to_event(self, seq: int) -> VisualizationDeltaEntry:
        # The payload is copied, so a producer mutating its card
        # after the append does not change the stored entry.
        return VisualizationDeltaEntry(
            seq=seq, run_id=self.run_id, payload=self.payload.model_copy()
        )


class WorkflowEventDraft(SessionEventDraftBase):
    """A workflow runtime record on its way into the log."""

    type: Literal["workflow_event"] = "workflow_event"
    payload: WorkflowEventRecord

    def to_event(self, seq: int) -> WorkflowEventEntry:
        # The payload is copied, so a producer mutating its record
        # after the append does not change the stored entry.
        return WorkflowEventEntry(seq=seq, run_id=self.run_id, payload=self.payload.model_copy())


#: A draft — one of the kinds above, told apart by its ``type``.
SessionEventDraft = Annotated[
    VisualizationDeltaDraft | WorkflowEventDraft,
    Field(discriminator="type"),
]


# ── Entries — what the log holds ──────────────────────────────


class SessionEventBase(BaseModel):
    """Common shape of every entry in the session event log.

    - ``seq`` — monotonic per-session counter. FE replay relies on
      this (not ``timestamp_ms``) because wall-clock is subject to
      skew and same-millisecond collisions.
    - ``run_id`` — the run that emitted the event. Empty string
      for boot-level events with no owning run.
    - ``timestamp_ms`` — Unix epoch milliseconds; only for FE
      display, never for ordering.
    """

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    run_id: str = ""
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))

    @field_validator("run_id", mode="before")
    @classmethod
    def _run_id_as_text(cls, value: object) -> str:
        return "" if value is None else str(value)


class VisualizationDeltaEntry(SessionEventBase):
    """A ``visualization_delta`` entry in the session event log.

    The payload is the viz card as it was sent; the dump keeps the
    wire key for the spec (``json``), so a row written here reads
    back the way the frontend received it.
    """

    type: Literal["visualization_delta"] = "visualization_delta"
    payload: VisualizationDeltaPayload

    @field_serializer("payload")
    def _dump_payload(self, payload: VisualizationDeltaPayload) -> dict[str, Any]:
        return payload.model_dump(by_alias=True)


class WorkflowEventEntry(SessionEventBase):
    """A ``workflow_event`` entry in the session event log.

    The payload is the workflow runtime's own record — the shape the
    frontend also receives on the ``workflow_event`` push channel.
    """

    type: Literal["workflow_event"] = "workflow_event"
    payload: WorkflowEventRecord

    @field_serializer("payload")
    def _dump_payload(self, payload: WorkflowEventRecord) -> dict[str, Any]:
        return payload.model_dump()


#: One entry in the log — one of the kinds above, told apart by its
#: ``type``.
SessionEvent = Annotated[
    VisualizationDeltaEntry | WorkflowEventEntry,
    Field(discriminator="type"),
]

#: Parses a persisted row back into the union. A row this build
#: doesn't model — a kind it doesn't know, or a shape that drifted
#: — fails validation, and the store drops that row rather than
#: handing a splicer an untyped blob.
SESSION_EVENT_ADAPTER: TypeAdapter[SessionEvent] = TypeAdapter(SessionEvent)


class SessionEventAppendResult(BaseModel):
    """What one append to the session log did.

    The append itself cannot fail: the event is in the in-memory log
    before the write is attempted, so live listeners see it either
    way. ``persisted`` says whether it also reached the store —
    ``False`` when the session has no persister yet, or when the
    write failed. ``error`` carries the reason when it did.
    """

    persisted: bool = False
    error: str | None = None


__all__ = [
    "SESSION_EVENT_ADAPTER",
    "SessionEvent",
    "SessionEventAppendResult",
    "SessionEventBase",
    "SessionEventDraft",
    "SessionEventDraftBase",
    "SessionEventKind",
    "VisualizationDeltaDraft",
    "VisualizationDeltaEntry",
    "VisualizationDeltaPayload",
    "WorkflowEventDraft",
    "WorkflowEventEntry",
]
