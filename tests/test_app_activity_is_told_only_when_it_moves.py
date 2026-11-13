"""``app.activity_changed``: told when the activity is not what it was.

:class:`ActivityWatch` re-reads the backend's activity after the messages
after which igni may have started or stopped doing something, tells the
change once, lets other listeners hear it, and says nothing when the
activity cannot be read.
"""

from __future__ import annotations

from typing import Any

import pytest

from igni_core.backend.activity import Activity, ActivityState
from igni_core.extensions.host.activity import ACTIVITY_CHANGED, ActivityWatch
from igni_core.protocol import messages as msg

MOVING = [
    msg.RunStarted(),
    msg.RunCompleted(),
    msg.RunError(error="boom"),
    msg.RunPaused(),
    msg.HITLRequest(requirement_id="r1"),
    msg.RequirementResolved(requirement_id="r1"),
    msg.StreamEnd(),
    msg.StreamingDone(),
]

IDLE = Activity(state=ActivityState.IDLE, suspendable=True)
RUNNING = Activity(state=ActivityState.RUNNING, suspendable=False)


class Backend:
    """A backend whose activity is whatever a test sets."""

    def __init__(self, activity: Activity | Exception = IDLE) -> None:
        self.activity = activity
        self.reads = 0

    def __call__(self) -> Activity:
        self.reads += 1
        if isinstance(self.activity, Exception):
            raise self.activity
        return self.activity


class Told:
    """What was emitted."""

    def __init__(self) -> None:
        self.got: list[tuple[str, Any]] = []

    def __call__(self, wire: str, value: Any = None, **_: Any) -> list[str]:
        self.got.append((wire, value))
        return []


def watch(backend: Backend) -> tuple[ActivityWatch, Told]:
    told = Told()
    return ActivityWatch(backend, told), told


def test_the_eight_messages_that_may_move_the_activity_are_the_whole_list() -> None:
    assert {type(m) for m in MOVING} == ActivityWatch.MOVES
    assert len(ActivityWatch.MOVES) == 8


@pytest.mark.parametrize("message", MOVING, ids=lambda m: type(m).__name__)
def test_each_of_those_messages_reads_the_activity_and_tells_it(message: msg.Message) -> None:
    backend = Backend(RUNNING)
    activity, told = watch(backend)

    activity.after(message)

    assert backend.reads == 1
    assert told.got == [(ACTIVITY_CHANGED, RUNNING.model_dump(mode="json"))]


def test_any_other_message_does_nothing() -> None:
    backend = Backend(RUNNING)
    activity, told = watch(backend)

    activity.after(msg.Cancel())
    activity.after(msg.Command(text="/help"))

    assert backend.reads == 0
    assert told.got == []


def test_a_subclass_of_a_moving_message_does_not_count() -> None:
    class Special(msg.RunStarted):
        pass

    backend = Backend(RUNNING)
    activity, told = watch(backend)

    activity.after(Special())

    assert backend.reads == 0 and told.got == []


def test_the_same_activity_twice_is_told_once_and_a_change_is_told_again() -> None:
    backend = Backend(RUNNING)
    activity, told = watch(backend)

    activity.moved()
    activity.moved()
    backend.activity = IDLE
    activity.moved()
    activity.moved()
    backend.activity = RUNNING
    activity.moved()

    assert [value["state"] for _, value in told.got] == ["running", "idle", "running"]


def test_an_activity_that_cannot_be_read_tells_nothing_and_keeps_the_last_one() -> None:
    backend = Backend(RUNNING)
    activity, told = watch(backend)
    activity.moved()

    backend.activity = RuntimeError("the backend is going away")
    activity.moved()
    backend.activity = RUNNING
    activity.moved()

    assert len(told.got) == 1, "unreadable is not a change, and does not reset what was told"


def test_listeners_hear_each_change_once_and_one_that_raises_does_not_stop_the_rest() -> None:
    backend = Backend(RUNNING)
    activity, told = watch(backend)
    heard: list[ActivityState] = []

    def broken(_: Activity) -> None:
        raise RuntimeError("listener bug")

    activity.listen(broken)
    activity.listen(lambda a: heard.append(a.state))

    activity.moved()
    activity.moved()
    backend.activity = IDLE
    activity.moved()

    assert heard == [ActivityState.RUNNING, ActivityState.IDLE]
    assert len(told.got) == 2


def test_a_listener_added_while_listeners_are_told_hears_the_next_change_only() -> None:
    backend = Backend(RUNNING)
    activity, _ = watch(backend)
    late: list[ActivityState] = []

    activity.listen(lambda _: activity.listen(lambda a: late.append(a.state)))
    activity.moved()
    assert late == []

    backend.activity = IDLE
    activity.moved()
    assert late == [ActivityState.IDLE]
