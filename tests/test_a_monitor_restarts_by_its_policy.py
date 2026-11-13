"""RestartPolicyEvaluator decisions and the MonitorSupervisor loop."""

from __future__ import annotations

import asyncio
import sys

import pytest

from igni_core.core.monitors import (
    MonitorConfig,
    MonitorHandle,
    MonitorStatus,
    MonitorSupervisor,
    RestartPolicyEvaluator,
)
from igni_core.core.monitors.models import BackoffDecision, GiveUpDecision, StopDecision

PY = sys.executable
GIVE_UP = "[monitor exceeded 4 restart attempts — giving up]"


def test_never_always_stops():
    ev = RestartPolicyEvaluator()
    for code in (0, 1, -9, None):
        assert isinstance(ev.decide("never", code, 0), StopDecision)


@pytest.mark.parametrize("code", [0, None])
def test_on_crash_stops_on_a_clean_or_unknown_exit(code):
    assert isinstance(RestartPolicyEvaluator().decide("on_crash", code, 0), StopDecision)


@pytest.mark.parametrize("code", [1, 137, -9])
def test_on_crash_backs_off_after_a_crash(code):
    decision = RestartPolicyEvaluator().decide("on_crash", code, 0)
    assert isinstance(decision, BackoffDecision)
    assert (decision.delay_seconds, decision.attempt) == (1.0, 1)


@pytest.mark.parametrize("code", [0, None, 1])
def test_always_restarts_whatever_the_exit(code):
    assert isinstance(RestartPolicyEvaluator().decide("always", code, 0), BackoffDecision)


def test_the_schedule_is_one_two_five_fifteen_then_give_up():
    ev = RestartPolicyEvaluator()
    seen = []
    for crashes in range(4):
        d = ev.decide("on_crash", 1, crashes)
        assert isinstance(d, BackoffDecision)
        seen.append((d.delay_seconds, d.attempt))
    assert seen == [(1.0, 1), (2.0, 2), (5.0, 3), (15.0, 4)]
    for crashes in (4, 5, 100):
        d = ev.decide("on_crash", 1, crashes)
        assert isinstance(d, GiveUpDecision)
        assert d.reason == GIVE_UP


def test_a_custom_schedule_sets_max_attempts_and_the_give_up_text():
    ev = RestartPolicyEvaluator(backoff_schedule=(0.5,))
    assert ev.max_attempts == 1
    assert RestartPolicyEvaluator().max_attempts == 4
    assert isinstance(ev.decide("always", 1, 0), BackoffDecision)
    d = ev.decide("always", 1, 1)
    assert isinstance(d, GiveUpDecision)
    assert "exceeded 1 restart attempts" in d.reason


def test_an_empty_schedule_gives_up_at_once():
    d = RestartPolicyEvaluator(backoff_schedule=()).decide("always", 0, 0)
    assert isinstance(d, GiveUpDecision)


def _handle(tmp_path, code: str, restart: str, command: str = PY) -> MonitorHandle:
    cfg = MonitorConfig(name="m", command=command, args=["-c", code], restart=restart)
    return MonitorHandle(cfg, project_dir=tmp_path)


async def _run(handle: MonitorHandle, schedule=(0.01, 0.01)) -> None:
    await handle.start()
    await asyncio.wait_for(
        MonitorSupervisor(handle, RestartPolicyEvaluator(schedule)).run(), timeout=20
    )


@pytest.mark.asyncio
async def test_a_clean_exit_under_on_crash_ends_stopped_with_its_code(tmp_path):
    h = _handle(tmp_path, "pass", "on_crash")
    await _run(h)
    assert h.status is MonitorStatus.STOPPED
    assert h.exit_code == 0
    assert h.crash_count == 0


@pytest.mark.asyncio
async def test_a_crash_is_annotated_counted_and_relaunched_then_given_up(tmp_path):
    h = _handle(tmp_path, "import sys; print('boom'); sys.exit(3)", "on_crash")
    await _run(h)
    out = h.output_tail(100)
    assert out.count("[monitor exited (code=3); restarting in 0s]") == 2
    assert out.count("boom") == 3  # first run plus two relaunches
    assert h.crash_count == 2
    assert h.status is MonitorStatus.FAILED
    assert out[-1] == "[monitor exceeded 2 restart attempts — giving up]"
    assert h.exit_code == 3


@pytest.mark.asyncio
async def test_always_restarts_a_clean_exit(tmp_path):
    h = _handle(tmp_path, "pass", "always")
    await _run(h, schedule=(0.01,))
    assert h.crash_count == 1
    assert h.status is MonitorStatus.FAILED


@pytest.mark.asyncio
async def test_a_relaunch_that_fails_ends_the_loop(tmp_path):
    h = _handle(tmp_path, "import sys; sys.exit(1)", "on_crash")
    await h.start()
    # Break the command after the first run so the relaunch cannot start.
    h.config.command = "/no/such/binary_xyz"
    await asyncio.wait_for(
        MonitorSupervisor(h, RestartPolicyEvaluator((0.01, 0.01))).run(), timeout=20
    )
    assert h.status is MonitorStatus.FAILED
    assert "failed to launch" in "\n".join(h.output_tail(10))
    assert h.crash_count == 1


@pytest.mark.asyncio
async def test_a_deliberate_stop_is_not_restarted(tmp_path):
    h = _handle(tmp_path, "import time; time.sleep(60)", "always")
    await h.start()
    task = asyncio.create_task(MonitorSupervisor(h, RestartPolicyEvaluator((0.01,))).run())
    await asyncio.sleep(0.1)
    await h.stop()
    await asyncio.wait_for(task, timeout=10)
    assert h.status is MonitorStatus.STOPPED
    assert h.crash_count == 0


@pytest.mark.asyncio
async def test_cancelling_the_supervisor_propagates(tmp_path):
    h = _handle(tmp_path, "import time; time.sleep(60)", "always")
    await h.start()
    task = asyncio.create_task(MonitorSupervisor(h).run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await h.stop()


@pytest.mark.asyncio
async def test_a_monitor_that_never_launched_stays_failed_under_supervision(tmp_path):
    cfg = MonitorConfig(name="m", command="/no/such/binary_xyz", restart="on_crash")
    h = MonitorHandle(cfg, project_dir=tmp_path)
    await h.start()
    await asyncio.wait_for(MonitorSupervisor(h).run(), timeout=10)
    assert h.status is MonitorStatus.FAILED
    assert h.crash_count == 0
