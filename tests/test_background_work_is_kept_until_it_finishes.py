"""``BackgroundWork``: what the host started is kept until it ends, then forgotten."""

from __future__ import annotations

import asyncio
import inspect

from igni_core.extensions.host.work import BackgroundWork


async def test_work_is_busy_while_it_runs_and_idle_once_it_finished() -> None:
    work = BackgroundWork()
    gate = asyncio.Event()
    assert work.idle()

    async def job() -> None:
        await gate.wait()

    work.spawn(job())
    assert not work.idle()

    gate.set()
    await work.drain()
    assert work.idle()


async def test_a_finished_job_is_forgotten_without_a_drain() -> None:
    work = BackgroundWork()
    done = asyncio.Event()

    async def job() -> None:
        done.set()

    work.spawn(job())
    await done.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert work.idle()


async def test_a_cancelled_job_is_forgotten() -> None:
    work = BackgroundWork()

    async def job() -> None:
        await asyncio.Event().wait()

    work.spawn(job())
    (task,) = list(work._tasks)
    task.cancel()
    await work.drain()

    assert work.idle()


async def test_a_drain_with_nothing_running_returns_at_once() -> None:
    await asyncio.wait_for(BackgroundWork().drain(), timeout=1)


async def test_a_failing_job_does_not_fail_the_drain_nor_stop_the_others() -> None:
    work = BackgroundWork()
    ran: list[str] = []

    async def bad() -> None:
        raise ValueError("boom")

    async def good() -> None:
        await asyncio.sleep(0.01)
        ran.append("good")

    work.spawn(bad())
    work.spawn(good())
    await work.drain()

    assert ran == ["good"]
    assert work.idle()


async def test_a_drain_waits_for_work_that_a_job_spawned_meanwhile() -> None:
    work = BackgroundWork()
    ran: list[str] = []

    async def child() -> None:
        await asyncio.sleep(0.01)
        ran.append("child")

    async def parent() -> None:
        await asyncio.sleep(0.01)
        work.spawn(child())

    work.spawn(parent())
    await work.drain()

    assert ran == ["child"]


def test_work_spawned_with_no_loop_is_closed_and_not_raised() -> None:
    work = BackgroundWork()

    async def job() -> None: ...

    coro = job()
    work.spawn(coro)

    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED
    assert work.idle()
