"""The background dispatches shutdown waits for.

A message is handled in a task the orchestrator spawns; on a graceful
exit :meth:`InFlightTasks.drain` lets every one finish, so no mid-stream
message is dropped, and one that failed does not stop the exit.
"""

from __future__ import annotations

import asyncio

from igni_core.backend.in_flight_tasks import InFlightTasks


async def test_a_spawned_task_runs_in_the_background_under_its_name() -> None:
    tasks = InFlightTasks()
    release = asyncio.Event()
    ran: list[str] = []

    async def work() -> str:
        await release.wait()
        ran.append("done")
        return "result"

    task = tasks.spawn(work(), name="my-dispatch")

    assert task.get_name() == "my-dispatch"
    assert not task.done() and ran == []
    assert tasks.pending() == [task]
    release.set()
    assert await task == "result"


async def test_a_task_with_no_name_is_called_dispatch() -> None:
    tasks = InFlightTasks()

    async def work() -> None:
        return None

    task = tasks.spawn(work())

    assert task.get_name() == "dispatch"
    await tasks.drain()


async def test_a_finished_task_is_no_longer_pending() -> None:
    tasks = InFlightTasks()

    async def quick() -> None:
        return None

    task = tasks.spawn(quick())
    await task
    await asyncio.sleep(0)

    assert tasks.pending() == []
    assert tasks._tasks == set()


async def test_drain_waits_for_a_slow_task() -> None:
    tasks = InFlightTasks()
    finished: list[str] = []

    async def slow() -> None:
        await asyncio.sleep(0.05)
        finished.append("slow")

    tasks.spawn(slow())
    await tasks.drain()

    assert finished == ["slow"]
    assert tasks.pending() == []


async def test_drain_with_nothing_spawned_returns_at_once() -> None:
    await asyncio.wait_for(InFlightTasks().drain(), timeout=1)


async def test_drain_does_not_raise_from_a_task_that_failed() -> None:
    tasks = InFlightTasks()
    finished: list[str] = []

    async def broken() -> None:
        raise RuntimeError("boom")

    async def fine() -> None:
        await asyncio.sleep(0.01)
        finished.append("fine")

    tasks.spawn(broken())
    tasks.spawn(fine())
    await tasks.drain()

    assert finished == ["fine"]
