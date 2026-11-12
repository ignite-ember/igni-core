"""A run's periodic checkpoint keeps going after one save fails.

Agno writes nothing between ``RunStarted`` and ``RunCompleted``; the
checkpointer is the only thing that gets a streaming answer onto disk. One
failed save (a locked database, a full disk) used to end the loop, so every
later tick of that run went unsaved without a word to the person.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any

from igni_core.backend.session_checkpointer import SessionCheckpointer


class Team:
    def __init__(self, session: Any = "blob", fail_first: int = 0) -> None:
        self.cached_session = session
        self.saved: list[Any] = []
        self.attempts = 0
        self.fail_first = fail_first

    async def asave_session(self, session: Any) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_first:
            raise RuntimeError("database is locked")
        self.saved.append(session)


async def run_for(checkpointer: SessionCheckpointer, seconds: float, **kwargs: Any) -> None:
    task = asyncio.create_task(checkpointer.run_forever(interval=0.01, **kwargs))
    await asyncio.sleep(seconds)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.done()


async def test_a_snapshot_saves_the_cached_session() -> None:
    team = Team(session="the-session")

    await SessionCheckpointer(team).snapshot()

    assert team.saved == ["the-session"]


async def test_a_snapshot_without_a_cached_session_saves_nothing() -> None:
    absent = Team(session=None)
    missing = SimpleNamespace(asave_session=absent.asave_session)

    await SessionCheckpointer(absent).snapshot()
    await SessionCheckpointer(missing).snapshot()

    assert absent.attempts == 0


async def test_a_snapshot_whose_save_fails_does_not_raise() -> None:
    team = Team(fail_first=1)

    await SessionCheckpointer(team).snapshot()

    assert team.attempts == 1 and team.saved == []


async def test_the_loop_saves_every_tick_when_no_hook_is_given() -> None:
    team = Team()

    await run_for(SessionCheckpointer(team), 0.1)

    assert len(team.saved) >= 3


async def test_a_save_that_fails_in_the_loop_is_tried_again_next_tick() -> None:
    team = Team(fail_first=1)

    await run_for(SessionCheckpointer(team), 0.1)

    assert team.attempts >= 3 and team.saved, "later ticks still saved"


async def test_a_hook_that_fails_does_not_end_the_loop_and_replaces_the_snapshot() -> None:
    team = Team()
    ticks: list[Any] = []

    async def hook(given: Any) -> None:
        ticks.append(given)
        if len(ticks) == 1:
            raise RuntimeError("hook failed")

    await run_for(SessionCheckpointer(team), 0.1, checkpoint_hook=hook)

    assert len(ticks) >= 3 and all(t is team for t in ticks)
    assert team.attempts == 0, "the hook stands in for the snapshot"


async def test_cancelling_ends_the_loop_quietly() -> None:
    task = asyncio.create_task(SessionCheckpointer(Team()).run_forever(interval=10))
    await asyncio.sleep(0)

    task.cancel()
    await task

    assert task.done() and not task.cancelled()
