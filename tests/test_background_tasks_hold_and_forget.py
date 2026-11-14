"""How started work is held, released and forgotten (``core/activity/background.py``).

A task that is cancelled or whose start fails must not leave igni busy, a
thread whose target raises must let go, and a finished task must not be
pinned.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from igni_core.core.activity import ActivityRegistry, BusyKind
from igni_core.core.activity.background import BackgroundTasks, background


@pytest.fixture
def tasks() -> BackgroundTasks:
    return BackgroundTasks(ActivityRegistry())


async def test_a_cancelled_task_lets_go(tasks: BackgroundTasks):
    task = tasks.start(asyncio.sleep(3600), holder="sleeper")
    assert tasks.registry.is_busy()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert tasks.registry.busy() == []


def test_a_start_that_cannot_be_scheduled_leaves_no_lease_and_raises(tasks: BackgroundTasks):
    async def work() -> None:
        pass

    coroutine = work()
    with pytest.raises(RuntimeError):
        tasks.start(coroutine, holder="x")
    coroutine.close()
    assert tasks.registry.busy() == []


async def test_a_started_task_is_named_after_its_holder_unless_named(tasks: BackgroundTasks):
    async def work() -> None:
        pass

    by_holder = tasks.start(work(), holder="indexer")
    named = tasks.start(work(), holder="indexer", name="custom")
    assert (by_holder.get_name(), named.get_name()) == ("indexer", "custom")
    await tasks.drain()


async def test_a_plain_awaitable_comes_back_as_a_future_and_is_busy(tasks: BackgroundTasks):
    inner: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    class Waiting:
        def __await__(self):
            return inner.__await__()

    future = tasks.start(Waiting(), holder="plain")
    assert isinstance(future, asyncio.Future)
    assert [lease.holder for lease in tasks.registry.busy()] == ["plain"]
    inner.set_result(7)
    assert await future == 7
    assert tasks.registry.busy() == []


async def test_a_daemon_on_a_plain_awaitable_is_tracked_but_not_busy(tasks: BackgroundTasks):
    inner: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    future = tasks.daemon(inner, name="d")
    assert future is inner
    assert tasks.registry.busy() == []
    drained = asyncio.ensure_future(tasks.drain())
    await asyncio.sleep(0)
    assert not drained.done(), "drain waits for it"
    inner.set_result(1)
    await drained


def test_work_can_be_started_on_an_explicit_loop_with_none_running(tasks: BackgroundTasks):
    loop = asyncio.new_event_loop()
    try:

        async def work() -> str:
            return "ok"

        future = tasks.start(work(), holder="far", loop=loop)
        assert future.get_loop() is loop
        assert [lease.holder for lease in tasks.registry.busy()] == ["far"]
        assert loop.run_until_complete(future) == "ok"
        assert tasks.registry.busy() == []
    finally:
        loop.close()


def test_a_daemon_can_be_started_on_an_explicit_loop_and_holds_no_lease(tasks: BackgroundTasks):
    loop = asyncio.new_event_loop()
    try:

        async def work() -> int:
            return 3

        future = tasks.daemon(work(), name="far-daemon", loop=loop)
        assert future.get_loop() is loop and tasks.registry.busy() == []
        assert loop.run_until_complete(future) == 3
    finally:
        loop.close()


async def test_start_without_a_loop_argument_uses_the_running_loop_for_a_coroutine(
    tasks: BackgroundTasks,
):
    async def work() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    assert await tasks.start(work(), holder="x") is asyncio.get_running_loop()


async def test_start_if_running_returns_the_task_and_holds_the_lease(tasks: BackgroundTasks):
    gate = asyncio.Event()

    async def work() -> None:
        await gate.wait()

    task = tasks.start_if_running(work(), holder="now", reason="why", kind=BusyKind.MONITOR)
    assert isinstance(task, asyncio.Task)
    [lease] = tasks.registry.busy()
    assert (lease.kind, lease.holder, lease.reason) == (BusyKind.MONITOR, "now", "why")
    gate.set()
    await task
    assert tasks.registry.busy() == []


def test_a_thread_whose_target_raises_still_lets_go(tasks: BackgroundTasks, monkeypatch):
    # The exception is the thread's own; keep pytest from reporting it as unhandled.
    monkeypatch.setattr(threading, "excepthook", lambda args: None)

    def boom() -> None:
        raise RuntimeError("boom")

    worker = tasks.thread(boom, name="crasher", counts=True, reason="r")
    worker.join(2)
    assert not worker.is_alive()
    assert tasks.registry.busy() == []


def test_a_thread_is_a_named_daemon_and_runs_its_target(tasks: BackgroundTasks):
    ran = threading.Event()
    worker = tasks.thread(ran.set, name="named")
    worker.join(2)
    assert ran.is_set() and worker.name == "named" and worker.daemon


async def test_a_lease_of_a_counted_thread_carries_its_name_reason_and_kind(
    tasks: BackgroundTasks,
):
    release = threading.Event()
    worker = tasks.thread(release.wait, name="w", counts=True, reason="because")
    [lease] = tasks.registry.busy()
    assert (lease.kind, lease.holder, lease.reason) == (BusyKind.BACKGROUND, "w", "because")
    release.set()
    worker.join(2)


async def test_drain_with_nothing_started_returns_at_once(tasks: BackgroundTasks):
    await asyncio.wait_for(tasks.drain(), 1)


async def test_drain_waits_for_work_and_for_what_that_work_starts(tasks: BackgroundTasks):
    order: list[str] = []

    async def child() -> None:
        await asyncio.sleep(0.01)
        order.append("child")

    async def parent() -> None:
        await asyncio.sleep(0.01)
        tasks.start(child(), holder="child")
        order.append("parent")

    tasks.start(parent(), holder="parent")
    await tasks.drain()
    assert order == ["parent", "child"]
    assert tasks.registry.busy() == []


async def test_drain_swallows_what_the_work_raised(tasks: BackgroundTasks):
    async def boom() -> None:
        raise RuntimeError("boom")

    future = tasks.start(boom(), holder="x")
    await tasks.drain()
    assert isinstance(future.exception(), RuntimeError)


async def test_a_finished_task_is_forgotten_and_a_running_one_is_held(tasks: BackgroundTasks):
    loop = asyncio.get_running_loop()
    gate = asyncio.Event()

    async def work() -> None:
        await gate.wait()

    task = tasks.start(work(), holder="x")
    assert task in tasks._bucket(loop)
    gate.set()
    await task
    await asyncio.sleep(0)
    assert task not in tasks._bucket(loop)


def test_each_loop_keeps_its_own_bucket(tasks: BackgroundTasks):
    first, second = asyncio.new_event_loop(), asyncio.new_event_loop()
    try:
        assert tasks._bucket(first) is tasks._bucket(first)
        assert tasks._bucket(first) is not tasks._bucket(second)
        assert tasks._bucket(first) == set()
    finally:
        first.close()
        second.close()


def test_the_shared_instance_is_one_and_falls_back_to_the_shared_registry():
    ActivityRegistry.reset_shared()
    assert BackgroundTasks.shared() is BackgroundTasks.shared() is background
    first = background.registry
    assert first is ActivityRegistry.shared()
    ActivityRegistry.reset_shared()
    assert background.registry is ActivityRegistry.shared() and background.registry is not first


def test_an_injected_registry_wins_over_the_shared_one():
    injected = ActivityRegistry()
    assert BackgroundTasks(injected).registry is injected
