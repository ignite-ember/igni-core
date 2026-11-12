"""A component action in a visualization is recorded and announced to every window.

The bus keeps the last 32 actions as a ring (oldest dropped, readers get a
copy) and broadcasts each one on ``visualization_action_dispatched`` through the
session's real broadcast bus; the caller gets the action and params echoed.
"""

from __future__ import annotations

from types import SimpleNamespace

from igni_core.backend.schemas_visualization import (
    VisualizationAction,
    VisualizationActionDispatchedEvent,
)
from igni_core.backend.visualization_action_bus import VisualizationActionBus
from igni_core.core.session.broadcast import BroadcastBus


def _bus(max_entries: int = 32) -> tuple[VisualizationActionBus, list]:
    broadcasts = BroadcastBus()
    seen: list = []
    broadcasts.register(seen.append)
    session = SimpleNamespace(broadcast=broadcasts.emit)
    return VisualizationActionBus(session, max_entries), seen  # type: ignore[arg-type]


def test_a_dispatch_is_echoed_recorded_and_broadcast_with_its_action_and_params() -> None:
    bus, seen = _bus()
    entry = VisualizationAction(action="approve", params={"id": 7, "tags": ["a", "é"]})

    result = bus.dispatch(entry)

    assert result.ok is True
    assert result.action == "approve"
    assert result.params == {"id": 7, "tags": ["a", "é"]}
    assert bus.actions == [entry]
    assert len(seen) == 1
    event = seen[0]
    assert isinstance(event, VisualizationActionDispatchedEvent)
    assert event.channel == "visualization_action_dispatched"
    assert event.action == "approve"
    assert event.params == {"id": 7, "tags": ["a", "é"]}


def test_an_action_without_params_is_dispatched_with_empty_params() -> None:
    bus, seen = _bus()

    result = bus.dispatch(VisualizationAction(action="close"))

    assert result.params == {}
    assert seen[0].params == {}


def test_the_ring_keeps_the_last_thirty_two_oldest_first() -> None:
    bus, seen = _bus()

    for i in range(40):
        bus.dispatch(VisualizationAction(action=f"a{i}"))

    assert [a.action for a in bus.actions] == [f"a{i}" for i in range(8, 40)]
    assert len(seen) == 40  # the ring forgets, the broadcast does not


def test_a_custom_ring_size_is_honoured() -> None:
    bus, _ = _bus(max_entries=2)

    for name in ("x", "y", "z"):
        bus.dispatch(VisualizationAction(action=name))

    assert [a.action for a in bus.actions] == ["y", "z"]


def test_the_actions_a_caller_reads_are_a_copy() -> None:
    bus, _ = _bus()
    bus.dispatch(VisualizationAction(action="one"))

    bus.actions.clear()
    bus.actions.append(VisualizationAction(action="forged"))

    assert [a.action for a in bus.actions] == ["one"]


def test_a_subscriber_that_raises_does_not_stop_the_dispatch() -> None:
    broadcasts = BroadcastBus()

    def boom(event) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("window gone")

    broadcasts.register(boom)
    bus = VisualizationActionBus(SimpleNamespace(broadcast=broadcasts.emit))  # type: ignore[arg-type]

    result = bus.dispatch(VisualizationAction(action="go"))

    assert result.ok is True
    assert [a.action for a in bus.actions] == ["go"]
