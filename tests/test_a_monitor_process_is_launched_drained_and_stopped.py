"""MonitorHandle: launch, output ring, snapshot, stop escalation."""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap

import pytest

from igni_core.core.monitors import MonitorConfig, MonitorHandle, MonitorStatus

PY = sys.executable


def _handle(tmp_path, code: str = "pass", **cfg) -> MonitorHandle:
    config = MonitorConfig(name="m", command=PY, args=["-c", code], restart="never", **cfg)
    return MonitorHandle(config, project_dir=tmp_path)


async def _wait_for(predicate, timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


def test_a_new_handle_is_stopped_and_empty(tmp_path):
    h = _handle(tmp_path)
    assert h.status is MonitorStatus.STOPPED
    assert (h.pid, h.exit_code, h.crash_count, h.uptime_seconds) == (None, None, 0, 0.0)
    assert not h.is_stopping()
    assert h.output_tail() == []
    snap = h.snapshot()
    assert (snap.name, snap.status, snap.pid, snap.uptime_seconds) == ("m", "stopped", None, 0.0)
    assert snap.restart == "never"
    assert snap.command == PY


@pytest.mark.asyncio
async def test_start_runs_in_the_resolved_cwd_with_the_env_and_closed_stdin(tmp_path):
    (tmp_path / "sub").mkdir()
    code = textwrap.dedent(
        """
        import os, sys
        print(os.getcwd(), flush=True)
        print(os.environ["IGNI_PROBE"], flush=True)
        print(repr(sys.stdin.read()), flush=True)
        print("to-stderr", file=sys.stderr)
        """
    )
    h = _handle(tmp_path, code, cwd="sub", env={"IGNI_PROBE": "yes"})
    await h.start()
    assert h.status is MonitorStatus.RUNNING
    assert h.pid is not None
    await _wait_for(lambda: "to-stderr" in h.output_tail())
    out = h.output_tail()
    assert os.path.samefile(out[0], tmp_path / "sub")
    assert "yes" in out
    assert "''" in out
    assert await h.wait_exit() == 0
    await h.stop()


@pytest.mark.asyncio
async def test_start_while_running_is_a_no_op(tmp_path):
    h = _handle(tmp_path, "import time; time.sleep(60)")
    await h.start()
    pid = h.pid
    await h.start()
    assert h.pid == pid
    await h.stop()


@pytest.mark.asyncio
async def test_a_launch_failure_fails_the_monitor_without_raising(tmp_path):
    cfg = MonitorConfig(name="m", command="/no/such/binary_xyz")
    h = MonitorHandle(cfg, project_dir=tmp_path)
    await h.start()
    assert h.status is MonitorStatus.FAILED
    assert h.pid is None
    assert h.output_tail()[0].startswith("[monitor failed to launch: ")


@pytest.mark.asyncio
async def test_a_missing_cwd_fails_the_launch(tmp_path):
    h = _handle(tmp_path, cwd="does/not/exist")
    await h.start()
    assert h.status is MonitorStatus.FAILED
    assert "failed to launch" in h.output_tail()[0]


@pytest.mark.asyncio
async def test_a_failed_relaunch_does_not_keep_the_old_pid(tmp_path):
    h = _handle(tmp_path, "pass")
    await h.start()
    assert h.pid is not None
    await h.wait_exit()
    h.config.command = "/no/such/binary_xyz"
    await h.relaunch_under_lock()
    assert h.status is MonitorStatus.FAILED
    assert h.pid is None
    assert h.snapshot().pid is None


@pytest.mark.asyncio
async def test_output_is_decoded_leniently_and_stripped_of_the_newline(tmp_path):
    code = (
        "import sys; sys.stdout.buffer.write(b'caf\\xc3\\xa9 \\xff bad\\nno-newline'); "
        "sys.stdout.buffer.flush()"
    )
    h = _handle(tmp_path, code)
    await h.start()
    await _wait_for(lambda: len(h.output_tail()) == 2)
    assert h.output_tail() == ["café � bad", "no-newline"]
    await h.stop()


@pytest.mark.asyncio
async def test_the_output_ring_keeps_the_last_thousand_lines(tmp_path):
    h = _handle(tmp_path, "for i in range(1200): print(i)")
    await h.start()
    await _wait_for(lambda: h.output_tail(1)[-1:] == ["1199"])
    everything = h.output_tail(5000)
    assert len(everything) == 1000
    assert everything[0] == "200"
    assert everything[-1] == "1199"
    await h.stop()


def test_output_tail_bounds_and_annotations(tmp_path):
    h = _handle(tmp_path)
    for i in range(5):
        h.append_output(f"l{i}")
    assert h.output_tail(2) == ["l3", "l4"]
    assert h.output_tail(50) == ["l0", "l1", "l2", "l3", "l4"]
    assert h.output_tail(0) == []
    assert h.output_tail(-3) == []
    assert h.output_tail() == ["l0", "l1", "l2", "l3", "l4"]


def test_status_transitions_and_counters(tmp_path):
    h = _handle(tmp_path)
    h.mark_running()
    assert h.status is MonitorStatus.RUNNING
    h.mark_stopped(exit_code=7)
    assert (h.status, h.exit_code) == (MonitorStatus.STOPPED, 7)
    h.mark_stopped()
    assert h.exit_code == 7  # no code given: keep the one recorded
    h.mark_running()
    assert h.exit_code is None  # a new run clears it
    h.mark_failed("why")
    assert h.status is MonitorStatus.FAILED
    assert h.output_tail() == ["why"]
    assert h.bump_crash_count() == 1
    assert h.bump_crash_count() == 2
    h.reset_crash_count()
    assert h.crash_count == 0
    h.bump_crash_count_to(9)
    assert h.crash_count == 9
    h.note_exit(-9)
    assert h.exit_code == -9
    assert h.status is MonitorStatus.FAILED  # note_exit leaves status alone


@pytest.mark.asyncio
async def test_a_live_snapshot_reports_pid_uptime_and_policy(tmp_path):
    h = _handle(tmp_path, "import time; time.sleep(60)")
    await h.start()
    await asyncio.sleep(0.05)
    snap = h.snapshot()
    assert snap.status == "running"
    assert snap.pid == h.pid
    assert snap.uptime_seconds == round(snap.uptime_seconds, 2)
    assert snap.uptime_seconds > 0
    await h.stop()
    assert h.snapshot().uptime_seconds == 0.0


@pytest.mark.asyncio
async def test_stop_terminates_and_records_the_signal_exit_code(tmp_path):
    h = _handle(tmp_path, "import time; time.sleep(60)")
    await h.start()
    await h.stop()
    assert h.status is MonitorStatus.STOPPED
    assert h.exit_code == -15
    assert not h.is_stopping()


@pytest.mark.asyncio
async def test_a_process_that_ignores_sigterm_is_killed(tmp_path, monkeypatch):
    from igni_core.core.monitors import handle as handle_module

    monkeypatch.setattr(handle_module, "_TERM_GRACE", 0.3)
    code = (
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready', flush=True); time.sleep(60)"
    )
    h = _handle(tmp_path, code)
    await h.start()
    await _wait_for(lambda: "ready" in h.output_tail())
    await h.stop()
    assert h.status is MonitorStatus.STOPPED
    assert h.exit_code == -9


@pytest.mark.asyncio
async def test_stop_without_a_process_or_after_it_exited_just_marks_stopped(tmp_path):
    h = _handle(tmp_path)
    h.mark_running()
    await h.stop()
    assert h.status is MonitorStatus.STOPPED
    assert not h.is_stopping()
    done = _handle(tmp_path, "pass")
    await done.start()
    await done.wait_exit()
    await done.stop()
    assert done.status is MonitorStatus.STOPPED
    await done.stop()  # twice is fine


@pytest.mark.asyncio
async def test_wait_exit_returns_the_code_or_none_when_never_launched(tmp_path):
    assert await _handle(tmp_path).wait_exit() is None
    h = _handle(tmp_path, "import sys; sys.exit(4)")
    await h.start()
    assert await h.wait_exit() == 4
    await h.stop()
