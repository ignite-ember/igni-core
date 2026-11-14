"""``AsyncFireAndForget.schedule`` from code that may or may not be in a loop.

The persistence hooks call it from synchronous code. With a running loop the
coroutine becomes a tracked background task and runs; with none, the
coroutine is closed (no "never awaited" warning), nothing is returned, and no
phantom loop is made for it — a phantom loop is what once leaked database
locks and hung a test session.
"""

from __future__ import annotations

import asyncio
import warnings

from igni_core.core.activity.registry import ActivityRegistry
from igni_core.core.tools.async_fire_and_forget import AsyncFireAndForget


async def test_in_a_running_loop_the_coroutine_runs_as_a_tracked_task():
    ran: list[str] = []
    gate = asyncio.Event()

    async def write() -> str:
        await gate.wait()
        ran.append("written")
        return "done"

    task = AsyncFireAndForget().schedule(write())

    assert isinstance(task, asyncio.Task)
    held = [
        lease for lease in ActivityRegistry.shared().busy() if lease.holder == "fire-and-forget"
    ]
    assert [lease.reason for lease in held] == ["a deferred write"]
    assert ran == []

    gate.set()
    assert await task == "done"
    assert ran == ["written"]
    await asyncio.sleep(0)  # let the done callback release the lease
    assert not [
        lease for lease in ActivityRegistry.shared().busy() if lease.holder == "fire-and-forget"
    ]


def test_with_no_loop_the_coroutine_is_closed_and_nothing_is_returned():
    ran: list[str] = []

    async def write() -> None:
        ran.append("written")

    coro = write()
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # a "never awaited" RuntimeWarning would raise
        assert AsyncFireAndForget().schedule(coro) is None
        del coro

    assert ran == []


def test_with_no_loop_the_coroutine_is_closed_not_left_suspended():
    async def write() -> None: ...

    coro = write()
    AsyncFireAndForget().schedule(coro)

    try:
        coro.send(None)
    except RuntimeError as exc:
        assert "cannot reuse already awaited coroutine" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("the coroutine was still runnable")


def test_with_no_loop_no_loop_is_created_for_it():
    async def write() -> None: ...

    AsyncFireAndForget().schedule(write())

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("a loop appeared")
