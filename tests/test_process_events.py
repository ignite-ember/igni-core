"""The process-lifecycle event models: frozen, discriminated on ``type``.

Subscribers get the model itself, never a mapping (the bus routes on
``type``), so what the models promise is what every subscriber relies on.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from igni_core.core.tools.process_bus import ProcessEventBus
from igni_core.core.tools.process_events import (
    ProcessEvent,
    ProcessExitEvent,
    ProcessLineEvent,
    ProcessStartEvent,
)

ADAPTER: TypeAdapter[ProcessEvent] = TypeAdapter(ProcessEvent)


class TestTheTypeDiscriminatorDefaults:
    def test_each_event_carries_its_own_type(self) -> None:
        assert ProcessStartEvent(pid=1, cmd="c", started_at=1.0).type == "start"
        assert ProcessLineEvent(pid=1, line="l").type == "line"
        exit_event = ProcessExitEvent(
            pid=1, cmd="c", exit_code=0, duration_seconds=1.0, output_tail=""
        )
        assert exit_event.type == "exit"

    def test_the_type_cannot_be_set_to_another_events(self) -> None:
        with pytest.raises(ValidationError):
            ProcessLineEvent(type="start", pid=1, line="l")  # type: ignore[arg-type]


class TestFrozen:
    def test_a_start_event_cannot_be_changed(self) -> None:
        event = ProcessStartEvent(pid=1, cmd="c", started_at=1.0)
        with pytest.raises(ValidationError):
            event.pid = 2  # type: ignore[misc]

    def test_a_line_event_cannot_be_changed(self) -> None:
        event = ProcessLineEvent(pid=1, line="l")
        with pytest.raises(ValidationError):
            event.line = "other"  # type: ignore[misc]

    def test_an_exit_event_cannot_be_changed(self) -> None:
        event = ProcessExitEvent(pid=1, cmd="c", exit_code=0, duration_seconds=1.0, output_tail="")
        with pytest.raises(ValidationError):
            event.exit_code = 1  # type: ignore[misc]


class TestTheUnionRoutesOnType:
    def test_a_payload_validates_to_the_event_class_its_type_names(self) -> None:
        start = ADAPTER.validate_python({"type": "start", "pid": 1, "cmd": "c", "started_at": 2.5})
        line = ADAPTER.validate_python({"type": "line", "pid": 1, "line": "hi"})
        done = ADAPTER.validate_python(
            {
                "type": "exit",
                "pid": 1,
                "cmd": "c",
                "exit_code": 3,
                "duration_seconds": 1.5,
                "output_tail": "t",
            }
        )
        assert (type(start), type(line), type(done)) == (
            ProcessStartEvent,
            ProcessLineEvent,
            ProcessExitEvent,
        )
        assert done.exit_code == 3  # type: ignore[union-attr]

    def test_an_unknown_type_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            ADAPTER.validate_python({"type": "restart", "pid": 1})

    def test_a_missing_type_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            ADAPTER.validate_python({"pid": 1, "line": "hi"})

    def test_a_missing_field_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            ADAPTER.validate_python({"type": "line", "pid": 1})

    def test_a_pid_that_is_not_an_integer_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            ADAPTER.validate_python({"type": "line", "pid": "abc", "line": "hi"})

    def test_an_event_round_trips_through_its_dump(self) -> None:
        event = ProcessExitEvent(pid=9, cmd="make", exit_code=2, duration_seconds=4.0, output_tail="x")
        assert ADAPTER.validate_python(event.model_dump()) == event


class TestExitEvent:
    def test_an_orphan_has_no_exit_code(self) -> None:
        event = ProcessExitEvent(
            pid=1, cmd="c", exit_code=None, duration_seconds=0.0, output_tail=""
        )
        assert event.exit_code is None

    def test_a_negative_code_is_a_signal_and_is_kept(self) -> None:
        event = ProcessExitEvent(pid=1, cmd="c", exit_code=-9, duration_seconds=0.0, output_tail="")
        assert event.exit_code == -9


class TestSubscribersReceiveTheModel:
    def test_each_subscriber_is_handed_the_model_not_a_mapping(self) -> None:
        bus = ProcessEventBus()
        got: list[object] = []
        bus.on("line", got.append)
        bus.emit(ProcessLineEvent(pid=1, line="日本語 " + "x" * 10_000))
        (event,) = got
        assert isinstance(event, ProcessLineEvent)
        assert event.line.startswith("日本語 ") and len(event.line) == 10_004

    def test_a_model_built_without_validation_is_the_same_event(self) -> None:
        # The hot path may use ``model_construct``; it must behave alike.
        built = ProcessLineEvent.model_construct(pid=1, line="l")
        assert built.type == "line" and built.pid == 1 and built.line == "l"
