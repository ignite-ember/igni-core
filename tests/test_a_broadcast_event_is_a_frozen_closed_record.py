"""The base every push-channel event builds on: frozen, closed, and serialised as JSON.

``core/broadcast_schema.py``. The events' payloads are what the window
and the SDKs read off the wire, so a typo in a field name must fail where
the event is built, not reach a client as a silently missing key.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

import pytest
from pydantic import ValidationError

from igni_core.core.broadcast_schema import (
    BroadcastEvent,
    BroadcastSink,
    RunScopedBroadcastEvent,
)


class _Colour(str, Enum):
    RED = "red"


class _Rich(BroadcastEvent):
    channel: Literal["rich"] = "rich"
    at: datetime
    colour: _Colour


class _Scoped(RunScopedBroadcastEvent):
    channel: Literal["scoped"] = "scoped"
    n: int = 0


class TestTheBase:
    def test_an_event_cannot_be_changed_after_it_is_built(self):
        event = _Scoped(n=1)
        with pytest.raises(ValidationError):
            event.n = 2  # type: ignore[misc]
        assert event.n == 1

    def test_a_field_the_event_does_not_declare_is_refused(self):
        with pytest.raises(ValidationError, match="extra_forbidden|Extra inputs"):
            _Scoped(n=1, typo=2)  # type: ignore[call-arg]

    def test_the_payload_is_json_safe_and_carries_no_channel(self):
        event = _Rich(at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc), colour=_Colour.RED)
        assert event.to_payload() == {"at": "2026-01-02T03:04:05Z", "colour": "red"}

    def test_an_event_that_is_not_run_scoped_comes_back_unchanged_from_a_stamp(self):
        event = _Rich(at=datetime(2026, 1, 1, tzinfo=timezone.utc), colour=_Colour.RED)
        assert event.with_run_id("R-1") is event
        assert event.with_run_id(None) is event


class TestRunScoping:
    def test_a_missing_run_id_is_not_in_the_payload_and_a_stamped_one_is(self):
        assert _Scoped().to_payload() == {"n": 0}
        assert _Scoped().with_run_id("R-1").to_payload() == {"n": 0, "run_id": "R-1"}

    def test_a_stamp_is_a_copy_of_the_same_type_and_leaves_the_original(self):
        event = _Scoped(n=4)
        stamped = event.with_run_id("R-1")
        assert type(stamped) is _Scoped and stamped.n == 4
        assert event.run_id is None

    def test_a_run_id_already_on_the_event_wins(self):
        event = _Scoped(run_id="first")
        assert event.with_run_id("second") is event
        assert event.to_payload()["run_id"] == "first"

    @pytest.mark.parametrize("empty", [None, ""])
    def test_an_empty_stamp_changes_nothing(self, empty):
        event = _Scoped()
        assert event.with_run_id(empty) is event


class TestTheSink:
    def test_a_callable_is_a_sink(self):
        assert isinstance(lambda event: None, BroadcastSink)

    def test_something_that_cannot_be_called_is_not(self):
        assert not isinstance(object(), BroadcastSink)
        assert not isinstance("emit", BroadcastSink)
