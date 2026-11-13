"""MonitorManager: listing, snapshots, start/restart/stop results, leases."""

from __future__ import annotations

import asyncio
import sys

import pytest

from igni_core.core.activity import ActivityRegistry, BusyKind
from igni_core.core.monitors import (
    MonitorConfig,
    MonitorControlResult,
    MonitorManager,
    MonitorStatus,
)

PY = sys.executable
SLEEP = ["-c", "import time; time.sleep(60)"]


def _cfg(name: str, args=None, restart: str = "never", command: str = PY) -> MonitorConfig:
    return MonitorConfig(name=name, command=command, args=args or SLEEP, restart=restart)


async def _wait_for(predicate, timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


def test_names_are_sorted_and_membership_is_answered(tmp_path):
    m = MonitorManager({"b": _cfg("b"), "a": _cfg("a")}, tmp_path)
    assert m.list_names() == ["a", "b"]
    assert m.is_configured("a")
    assert not m.is_configured("zzz")
    assert MonitorManager({}, tmp_path).list_names() == []


def test_the_configs_are_copied_on_construction(tmp_path):
    configs = {"a": _cfg("a")}
    m = MonitorManager(configs, tmp_path)
    configs["late"] = _cfg("late")
    assert m.list_names() == ["a"]


def test_unstarted_monitors_show_as_stopped_in_sorted_order(tmp_path):
    m = MonitorManager(
        {"z": _cfg("z", restart="always"), "a": _cfg("a", restart="on_crash")}, tmp_path
    )
    snaps = m.snapshot_all()
    assert [s.name for s in snaps] == ["a", "z"]
    assert [s.restart for s in snaps] == ["on_crash", "always"]
    for s in snaps:
        assert (s.status, s.pid, s.uptime_seconds, s.exit_code, s.crash_count) == (
            MonitorStatus.STOPPED,
            None,
            0.0,
            None,
            0,
        )


def test_output_of_an_unknown_or_unstarted_monitor_is_empty(tmp_path):
    m = MonitorManager({"a": _cfg("a")}, tmp_path)
    assert m.output_tail("nope") == []
    assert m.output_tail("a") == []


@pytest.mark.asyncio
async def test_unknown_names_are_refused_with_the_reason(tmp_path):
    m = MonitorManager({}, tmp_path)
    for action in ("restart", "stop"):
        result = await getattr(m, action)("ghost")
        assert result == MonitorControlResult(
            ok=False, name="ghost", action=action, reason="Monitor not configured: 'ghost'"
        )


@pytest.mark.asyncio
async def test_start_all_is_idempotent_and_each_supervisor_holds_a_monitor_lease(tmp_path):
    m = MonitorManager({"a": _cfg("a"), "b": _cfg("b")}, tmp_path)
    await m.start_all()
    try:
        pids = {s.name: s.pid for s in m.snapshot_all()}
        assert all(pids.values())
        sups = dict(m._supervisors)
        held = {(r.holder, r.kind) for r in ActivityRegistry.shared().busy()}
        assert ("monitor:a", BusyKind.MONITOR) in held
        assert ("monitor:b", BusyKind.MONITOR) in held
        await m.start_all()
        assert {s.name: s.pid for s in m.snapshot_all()} == pids
        assert m._supervisors == sups
    finally:
        await m.shutdown_all()


@pytest.mark.asyncio
async def test_a_finished_supervisor_is_replaced_on_the_next_start(tmp_path):
    m = MonitorManager({"a": _cfg("a", ["-c", "pass"])}, tmp_path)
    await m.start_all()
    first = m._supervisors["a"]
    await _wait_for(first.done)
    await m.start_all()
    assert m._supervisors["a"] is not first
    await m.shutdown_all()


@pytest.mark.asyncio
async def test_stop_reports_success_and_a_never_started_monitor_stops_too(tmp_path):
    m = MonitorManager({"a": _cfg("a"), "idle": _cfg("idle")}, tmp_path)
    await m.start_all()
    result = await m.stop("a")
    assert result == MonitorControlResult(ok=True, name="a", action="stop", reason="Stopped a.")
    assert str(result) == "Stopped a."
    assert m.snapshot_all()[0].status == "stopped"
    fresh = MonitorManager({"idle": _cfg("idle")}, tmp_path)
    assert (await fresh.stop("idle")).ok
    await m.shutdown_all()


@pytest.mark.asyncio
async def test_a_deliberate_stop_is_not_undone_by_the_restart_policy(tmp_path):
    m = MonitorManager({"a": _cfg("a", restart="always")}, tmp_path)
    await m.start_all()
    pid = m.snapshot_all()[0].pid
    await m.stop("a")
    await asyncio.sleep(1.3)  # longer than the first backoff step
    snap = m.snapshot_all()[0]
    assert snap.status == "stopped"
    assert snap.crash_count == 0
    assert "a" not in m._supervisors
    assert pid is not None
    await m.shutdown_all()


@pytest.mark.asyncio
async def test_restart_revives_a_failed_monitor_and_forgets_its_crashes(tmp_path):
    m = MonitorManager({"a": _cfg("a")}, tmp_path)
    await m.start_all()
    h = m._handles["a"]
    old_pid = h.pid
    h.bump_crash_count_to(3)
    h.mark_failed("[forced]")
    result = await m.restart("a")
    assert result == MonitorControlResult(
        ok=True, name="a", action="restart", reason="Restarted a."
    )
    assert h.crash_count == 0
    assert h.status is MonitorStatus.RUNNING
    assert h.pid != old_pid
    assert "a" in m._supervisors
    await m.shutdown_all()


@pytest.mark.asyncio
async def test_restart_of_a_never_started_monitor_starts_it(tmp_path):
    m = MonitorManager({"a": _cfg("a")}, tmp_path)
    assert (await m.restart("a")).ok
    assert m.snapshot_all()[0].status == "running"
    await m.shutdown_all()


@pytest.mark.asyncio
async def test_a_launch_failure_in_start_all_does_not_raise_or_block_the_others(tmp_path):
    m = MonitorManager(
        {"a": _cfg("a", command="/no/such/xyz"), "b": _cfg("b")},
        tmp_path,
    )
    await m.start_all()
    await _wait_for(m._supervisors["a"].done)  # the supervisor has seen it
    snaps = {s.name: s.status for s in m.snapshot_all()}
    assert snaps == {"a": "failed", "b": "running"}
    await m.shutdown_all()


@pytest.mark.asyncio
async def test_shutdown_stops_everything_and_clears_state(tmp_path):
    m = MonitorManager({"a": _cfg("a")}, tmp_path)
    await MonitorManager({}, tmp_path).shutdown_all()  # no handles: fine
    await m.start_all()
    pid = m.snapshot_all()[0].pid
    await m.shutdown_all()
    assert m._handles == {}
    assert m._supervisors == {}
    assert m.snapshot_all()[0].pid is None
    assert pid is not None
    await m.shutdown_all()
