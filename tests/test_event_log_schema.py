"""Schema-level tests for the session event log's models.

The append path through ``Session.append_event`` is exercised
in ``test_event_log.py``; this file locks in the schema's own
invariants (validation, defaults, wire round-trip, the tagged
union, the defensive payload copy) so future edits to the schema
fail loud rather than silently drifting the shape.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from igni_core.backend.schemas_workflow_events import WorkflowEventRecord
from igni_core.core.session.event_log_schema import (
    SESSION_EVENT_ADAPTER,
    SessionEventDraft,
    VisualizationDeltaDraft,
    VisualizationDeltaEntry,
    VisualizationDeltaPayload,
    WorkflowEventDraft,
    WorkflowEventEntry,
)

_DRAFT_ADAPTER: TypeAdapter[SessionEventDraft] = TypeAdapter(SessionEventDraft)


def _record() -> WorkflowEventRecord:
    return WorkflowEventRecord(
        workflow_run_id="w1",
        name="run",
        ts=1,
        seq=1,
        type="workflow_started",
        payload={},
    )


class TestDrafts:
    def test_a_draft_becomes_an_event_at_its_seq(self):
        draft = VisualizationDeltaDraft(
            payload=VisualizationDeltaPayload(spec_id="s1", spec_json="{}"),
            run_id="run-x",
        )
        event = draft.to_event(42)
        assert event.seq == 42
        assert event.type == "visualization_delta"
        assert event.run_id == "run-x"
        assert event.payload.spec_id == "s1"
        assert event.payload.spec_json == "{}"

    def test_run_id_defaults_to_empty(self):
        draft = VisualizationDeltaDraft(payload=VisualizationDeltaPayload(spec_id="s1"))
        assert draft.to_event(1).run_id == ""

    def test_run_id_is_coerced_to_str(self):
        # ``run_id`` accepts anything with a ``__str__``; None → "".
        draft = VisualizationDeltaDraft(
            payload=VisualizationDeltaPayload(spec_id="s1"), run_id=None
        )  # type: ignore[arg-type]
        assert draft.to_event(1).run_id == ""
        draft = VisualizationDeltaDraft(payload=VisualizationDeltaPayload(spec_id="s1"), run_id=123)  # type: ignore[arg-type]
        assert draft.to_event(1).run_id == "123"

    def test_the_payload_is_copied_defensively(self):
        # Mutating the producer's card after the append must not
        # affect the stored payload.
        draft = VisualizationDeltaDraft(
            payload=VisualizationDeltaPayload(spec_id="s1", spec_json="{}")
        )
        event = draft.to_event(1)
        draft.payload.spec_json = "MUTATED"
        assert event.payload.spec_json == "{}"

    def test_the_draft_union_tells_the_kinds_apart(self):
        # The union is tagged: a draft is parsed by its ``type``.
        draft = _DRAFT_ADAPTER.validate_python(
            {"type": "workflow_event", "payload": _record().model_dump(), "run_id": "r1"}
        )
        assert isinstance(draft, WorkflowEventDraft)
        assert draft.to_event(1).type == "workflow_event"

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ValidationError):
            _DRAFT_ADAPTER.validate_python({"type": "nope", "payload": {}})


class TestValidation:
    def test_extra_field_rejected(self):
        # ``extra="forbid"`` — a stray field means producer drift
        # and should fail loud.
        with pytest.raises(ValidationError):
            VisualizationDeltaEntry(
                seq=1,
                payload=VisualizationDeltaPayload(spec_id="s1"),
                unexpected_field="value",  # type: ignore[call-arg]
            )

    def test_seq_must_be_positive(self):
        with pytest.raises(ValidationError):
            VisualizationDeltaEntry(seq=0, payload=VisualizationDeltaPayload(spec_id="s1"))

    def test_the_payload_is_required(self):
        with pytest.raises(ValidationError):
            VisualizationDeltaEntry(seq=1)  # type: ignore[call-arg]

    def test_timestamp_ms_auto_populated(self):
        event = VisualizationDeltaEntry(seq=1, payload=VisualizationDeltaPayload(spec_id="s1"))
        # Any positive int — we're not pinning the exact clock, just
        # that it's populated with something usable.
        assert isinstance(event.timestamp_ms, int)
        assert event.timestamp_ms > 0


class TestWireRoundTrip:
    def test_a_valid_row_round_trips(self):
        wire = {
            "seq": 5,
            "run_id": "r1",
            "timestamp_ms": 1_700_000_000_000,
            "type": "visualization_delta",
            "payload": {"spec_id": "s1", "json": "abc"},
        }
        event = SESSION_EVENT_ADAPTER.validate_python(wire)
        assert event.seq == 5
        assert event.type == "visualization_delta"
        assert isinstance(event, VisualizationDeltaEntry)
        assert event.payload.spec_json == "abc"
        # The card's payload is normalised: every field the card has
        # lands in the row, so a row written by an older build reads
        # back with the same keys the log writes today.
        dumped = event.model_dump()
        assert dumped["seq"] == 5
        assert dumped["run_id"] == "r1"
        assert dumped["type"] == "visualization_delta"
        assert dumped["payload"]["spec_id"] == "s1"
        assert dumped["payload"]["json"] == "abc"
        # And the round trip is stable.
        assert SESSION_EVENT_ADAPTER.validate_python(dumped).model_dump() == dumped

    def test_a_workflow_row_round_trips(self):
        record = _record()
        wire = {
            "seq": 2,
            "run_id": "r1",
            "timestamp_ms": 1_700_000_000_000,
            "type": "workflow_event",
            "payload": record.model_dump(),
        }
        event = SESSION_EVENT_ADAPTER.validate_python(wire)
        assert isinstance(event, WorkflowEventEntry)
        assert event.payload.workflow_run_id == "w1"
        assert event.model_dump() == wire

    def test_a_stale_row_is_refused(self):
        # A corrupt row from the DB shouldn't reach a splicer: the
        # adapter raises and the store drops the row.
        with pytest.raises(ValidationError):
            SESSION_EVENT_ADAPTER.validate_python({"not": "an event"})
        with pytest.raises(ValidationError):
            SESSION_EVENT_ADAPTER.validate_python({"seq": "not-an-int"})

    def test_a_row_of_an_unknown_kind_is_refused(self):
        # A kind this build doesn't model can't be read back — the
        # closed union says so instead of passing it through.
        with pytest.raises(ValidationError):
            SESSION_EVENT_ADAPTER.validate_python(
                {
                    "seq": 1,
                    "run_id": "",
                    "timestamp_ms": 0,
                    "type": "content_preview",
                    "payload": {},
                }
            )

    def test_an_extra_field_is_refused(self):
        # ``extra="forbid"`` propagates — a producer drift should
        # surface as a dropped row rather than a silently-passed
        # unknown field.
        with pytest.raises(ValidationError):
            SESSION_EVENT_ADAPTER.validate_python(
                {
                    "seq": 1,
                    "run_id": "",
                    "timestamp_ms": 0,
                    "type": "visualization_delta",
                    "payload": {},
                    "unknown": "value",
                }
            )


class TestModelDump:
    def test_dump_matches_pre_refactor_wire_shape(self):
        # The pre-iter-45 ``append_event`` built dicts of exactly
        # this shape. ``.model_dump()`` must produce the same keys /
        # types so the splicer + persistence path see no change.
        event = VisualizationDeltaDraft(
            payload=VisualizationDeltaPayload(spec_id="s1", spec_json="{}"),
            run_id="r1",
        ).to_event(1)
        dumped = event.model_dump()
        assert set(dumped.keys()) == {"seq", "run_id", "timestamp_ms", "type", "payload"}
        assert dumped["seq"] == 1
        assert dumped["run_id"] == "r1"
        assert dumped["type"] == "visualization_delta"
        # The spec keeps its wire key — ``json``, not ``spec_json``.
        assert dumped["payload"]["json"] == "{}"
        assert dumped["payload"]["spec_id"] == "s1"
        assert isinstance(dumped["timestamp_ms"], int)
