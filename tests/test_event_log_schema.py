"""Schema-level tests for :class:`SessionEvent`.

The append path through ``Session.append_event`` is exercised
in ``test_event_log.py``; this file locks in the model's own
invariants (validation, defaults, wire round-trip, defensive
payload copy) so future edits to the schema fail loud rather
than silently drifting the shape.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from igni_core.core.session.event_log_schema import SessionEvent, SessionEventDraft


class TestStamped:
    def test_stamped_preserves_fields(self):
        event = SessionEventDraft(
            type="visualization_delta",
            payload={"spec_id": "s1", "json": "{}"},
            run_id="run-x",
        ).stamped(42)
        assert event.seq == 42
        assert event.type == "visualization_delta"
        assert event.run_id == "run-x"
        assert event.payload == {"spec_id": "s1", "json": "{}"}

    def test_run_id_defaults_to_empty(self):
        event = SessionEventDraft(type="x").stamped(1)
        assert event.run_id == ""

    def test_run_id_is_coerced_to_str(self):
        # ``run_id`` accepts anything with a ``__str__``; None → "".
        assert SessionEventDraft(type="x", run_id=None).stamped(1).run_id == ""  # type: ignore[arg-type]
        assert SessionEventDraft(type="x", run_id=123).stamped(1).run_id == "123"  # type: ignore[arg-type]

    def test_stamped_copies_payload_defensively(self):
        # Mutating the producer's dict after the append must not
        # affect the stored payload.
        draft = SessionEventDraft(type="x", payload={"a": 1})
        event = draft.stamped(1)
        draft.payload["a"] = 999
        assert event.payload == {"a": 1}

    def test_of_dumps_a_model_payload(self):
        class Card(BaseModel):
            title: str

        draft = SessionEventDraft.of("extension_card", Card(title="Deploys"), run_id="r1")
        assert draft.payload == {"title": "Deploys"}
        assert draft.run_id == "r1"


class TestValidation:
    def test_extra_field_rejected(self):
        # ``extra="forbid"`` — a stray field means producer drift
        # and should fail loud.
        with pytest.raises(ValidationError):
            SessionEvent(
                seq=1,
                type="x",
                payload={},
                unexpected_field="value",  # type: ignore[call-arg]
            )

    def test_seq_must_be_positive(self):
        with pytest.raises(ValidationError):
            SessionEvent(seq=0, type="x", payload={})

    def test_type_required(self):
        with pytest.raises(ValidationError):
            SessionEvent(seq=1, payload={})  # type: ignore[call-arg]

    def test_payload_defaults_to_empty_dict(self):
        event = SessionEvent(seq=1, type="x")
        assert event.payload == {}

    def test_timestamp_ms_auto_populated(self):
        event = SessionEvent(seq=1, type="x")
        # Any positive int — we're not pinning the exact clock, just
        # that it's populated with something usable.
        assert isinstance(event.timestamp_ms, int)
        assert event.timestamp_ms > 0


class TestFromWire:
    def test_valid_dict_round_trips(self):
        wire = {
            "seq": 5,
            "run_id": "r1",
            "timestamp_ms": 1_700_000_000_000,
            "type": "visualization_delta",
            "payload": {"json": "abc"},
        }
        event = SessionEvent.from_wire(wire)
        assert event is not None
        assert event.seq == 5
        assert event.type == "visualization_delta"
        assert event.payload == {"json": "abc"}
        # Round-trip via ``model_dump`` produces the same shape.
        assert event.model_dump() == wire

    def test_malformed_returns_none(self):
        # A corrupt row from the DB shouldn't crash the log load.
        # Both "wrong type" and "missing required" cases return None.
        assert SessionEvent.from_wire({"not": "an event"}) is None
        assert SessionEvent.from_wire({"seq": "not-an-int"}) is None

    def test_extra_field_returns_none(self):
        # ``extra="forbid"`` propagates through from_wire — a producer
        # drift should surface as a dropped row rather than a
        # silently-passed-through unknown field.
        wire = {
            "seq": 1,
            "run_id": "",
            "timestamp_ms": 0,
            "type": "x",
            "payload": {},
            "unknown": "value",
        }
        assert SessionEvent.from_wire(wire) is None


class TestModelDump:
    def test_dump_matches_pre_refactor_wire_shape(self):
        # The pre-iter-45 ``append_event`` built dicts of exactly
        # this shape. ``.model_dump()`` must produce the same keys /
        # types so the splicer + persistence path see no change.
        event = SessionEventDraft(
            type="visualization_delta",
            payload={"spec_id": "s1", "json": "{}"},
            run_id="r1",
        ).stamped(1)
        dumped = event.model_dump()
        assert set(dumped.keys()) == {"seq", "run_id", "timestamp_ms", "type", "payload"}
        assert dumped["seq"] == 1
        assert dumped["run_id"] == "r1"
        assert dumped["type"] == "visualization_delta"
        assert dumped["payload"] == {"spec_id": "s1", "json": "{}"}
        assert isinstance(dumped["timestamp_ms"], int)
