"""``SessionStartupPhase``: the shared scheduling and logging helpers.

A phase is started from session boot, which may run before any event loop
exists. With a loop the work starts as accounted background work held by
``startup`` (so a suspended VM is not suspended under it); without one the
phase does nothing and never builds the coroutine.
"""

from __future__ import annotations

import asyncio
import logging
import warnings

from igni_core.core.activity import ActivityRegistry
from igni_core.core.session.startup import SessionStartupPhase


class _Phase(SessionStartupPhase):
    pass


def test_without_a_loop_nothing_is_scheduled_and_the_coroutine_is_never_built():
    built: list[int] = []

    async def work() -> None:
        pass

    def factory():
        built.append(1)
        return work()

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # an un-awaited coroutine would raise
        assert _Phase()._schedule_on_loop(factory) is False
    assert built == []


async def test_with_a_loop_the_work_runs_and_is_held_by_startup_under_the_class_name():
    ActivityRegistry.reset_shared()
    gate = asyncio.Event()
    done: list[int] = []

    async def work() -> None:
        await gate.wait()
        done.append(1)

    assert _Phase()._schedule_on_loop(work) is True
    leases = ActivityRegistry.shared().busy()
    assert [(lease.holder, lease.reason) for lease in leases] == [("startup", "_Phase")]

    gate.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert done == [1]
    assert ActivityRegistry.shared().busy() == []


async def test_a_factory_that_raises_inside_a_loop_reaches_the_caller():
    def factory():
        raise ValueError("cannot build")

    try:
        _Phase()._schedule_on_loop(factory)
    except ValueError as e:
        assert str(e) == "cannot build"
    else:
        raise AssertionError("the factory's error was swallowed")


def test_a_swallowed_failure_is_one_debug_line_naming_the_action_and_the_error(caplog):
    with caplog.at_level(logging.DEBUG, logger="igni_core.core.session.startup.base"):
        _Phase()._log_swallowed(RuntimeError("héllo ✓"), "marketplace refresh")
    [record] = caplog.records
    assert record.levelno == logging.DEBUG
    assert record.getMessage() == "marketplace refresh failed (héllo ✓); continuing"
