"""``ActivityNudge``: a lease taken on any thread, one look on the loop."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from igni_core.backend.activity_nudge import ActivityNudge


class _Watch:
    def __init__(self) -> None:
        self.looks: list[str] = []

    def moved(self) -> None:
        self.looks.append(threading.current_thread().name)


def _model(watch: _Watch):
    return SimpleNamespace(services=SimpleNamespace(activity_watch=watch))


async def test_a_burst_of_moves_before_the_loop_turns_is_one_look():
    watch = _Watch()
    nudge = ActivityNudge(_model(watch), asyncio.get_running_loop())

    for _ in range(5):
        nudge()
    assert watch.looks == []
    await asyncio.sleep(0)

    assert len(watch.looks) == 1


async def test_a_move_after_the_look_is_a_new_look():
    watch = _Watch()
    nudge = ActivityNudge(_model(watch), asyncio.get_running_loop())

    nudge()
    await asyncio.sleep(0)
    nudge()
    await asyncio.sleep(0)

    assert len(watch.looks) == 2


async def test_a_move_on_another_thread_is_looked_at_on_the_loop_thread():
    watch = _Watch()
    loop = asyncio.get_running_loop()
    nudge = ActivityNudge(_model(watch), loop)

    await loop.run_in_executor(None, nudge)
    await asyncio.sleep(0.05)

    assert watch.looks == [threading.current_thread().name]


def test_a_closed_loop_is_tolerated_silently():
    loop = asyncio.new_event_loop()
    loop.close()
    watch = _Watch()

    ActivityNudge(_model(watch), loop)()

    assert watch.looks == []
