"""Eviction timers for finished processes: the TTL and the per-pid tasks.

The registry owns the pid map; ``ProcessEviction`` owns when a finished
entry leaves it. It is driven here directly with a short real TTL and a
callback that records, so what is pinned is the timing contract itself.
"""

from __future__ import annotations

import asyncio

import pytest

from igni_core.core.tools.process_eviction import ProcessEviction

TTL = 0.05


async def _settle(seconds: float = TTL * 4) -> None:
    await asyncio.sleep(seconds)


class _Calls:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


def _tasks(eviction: ProcessEviction) -> dict[int, asyncio.Task[object]]:
    return eviction._tasks  # the handles are the observable state of the timers


async def test_the_callback_runs_once_the_ttl_has_passed() -> None:
    eviction = ProcessEviction(TTL)
    calls = _Calls()
    eviction.arm(1, calls)
    assert calls.count == 0
    await _settle()
    assert calls.count == 1


async def test_a_ttl_of_zero_expires_on_the_next_tick() -> None:
    eviction = ProcessEviction(0)
    calls = _Calls()
    eviction.arm(1, calls)
    await _settle(0.02)
    assert calls.count == 1


async def test_the_ttl_reads_back_and_changes() -> None:
    eviction = ProcessEviction(5.0)
    assert eviction.ttl_seconds == 5.0
    eviction.set_ttl_seconds(7.5)
    assert eviction.ttl_seconds == 7.5


async def test_re_arming_restarts_the_clock_and_expires_once() -> None:
    ttl = 0.3
    eviction = ProcessEviction(ttl)
    calls = _Calls()
    eviction.arm(1, calls)
    await asyncio.sleep(ttl * 0.65)
    eviction.arm(1, calls)  # most recent arm wins
    await asyncio.sleep(ttl * 0.65)
    assert calls.count == 0  # the first deadline passed; the second has not
    await _settle(ttl)
    assert calls.count == 1


async def test_cancel_prevents_the_expiry() -> None:
    eviction = ProcessEviction(TTL)
    calls = _Calls()
    eviction.arm(1, calls)
    eviction.cancel(1)
    await _settle()
    assert calls.count == 0
    assert _tasks(eviction) == {}


async def test_cancel_only_touches_its_own_pid() -> None:
    eviction = ProcessEviction(TTL)
    one, two = _Calls(), _Calls()
    eviction.arm(1, one)
    eviction.arm(2, two)
    eviction.cancel(1)
    await _settle()
    assert (one.count, two.count) == (0, 1)


async def test_cancel_of_an_unknown_or_finished_pid_is_a_no_op() -> None:
    eviction = ProcessEviction(0)
    eviction.cancel(99)
    calls = _Calls()
    eviction.arm(1, calls)
    await _settle(0.02)
    eviction.cancel(1)  # the task is done by now
    assert calls.count == 1


async def test_cancel_all_stops_every_timer_and_empties_the_map() -> None:
    eviction = ProcessEviction(TTL)
    calls = _Calls()
    for pid in (1, 2, 3):
        eviction.arm(pid, calls)
    eviction.cancel_all()
    await _settle()
    assert calls.count == 0
    assert _tasks(eviction) == {}


async def test_cancel_all_with_nothing_armed_is_fine() -> None:
    ProcessEviction(TTL).cancel_all()


async def test_forget_drops_the_handle_but_does_not_cancel() -> None:
    eviction = ProcessEviction(TTL)
    calls = _Calls()
    eviction.arm(1, calls)
    eviction.forget(1)
    assert _tasks(eviction) == {}
    await _settle()
    assert calls.count == 1  # still ran: forget is not cancel


async def test_forget_of_an_unknown_pid_is_a_no_op() -> None:
    ProcessEviction(TTL).forget(1)


async def test_a_changed_ttl_applies_to_the_next_arm_not_the_armed_task() -> None:
    eviction = ProcessEviction(TTL)
    calls = _Calls()
    eviction.arm(1, calls)
    eviction.set_ttl_seconds(60)
    await _settle()
    assert calls.count == 1  # armed under the short TTL
    later = _Calls()
    eviction.arm(2, later)
    await _settle()
    assert later.count == 0  # armed under the long one
    eviction.cancel_all()


async def test_the_timer_task_is_named_for_its_pid() -> None:
    eviction = ProcessEviction(60)
    eviction.arm(77, _Calls())
    assert _tasks(eviction)[77].get_name() == "process-evict-77"
    eviction.cancel_all()


def test_arming_with_no_running_loop_arms_nothing_and_does_not_raise() -> None:
    eviction = ProcessEviction(TTL)
    eviction.arm(1, _Calls())
    assert _tasks(eviction) == {}


@pytest.mark.filterwarnings("error::RuntimeWarning")
def test_arming_with_no_running_loop_leaves_no_unawaited_coroutine() -> None:
    import gc

    ProcessEviction(TTL).arm(1, _Calls())
    gc.collect()
