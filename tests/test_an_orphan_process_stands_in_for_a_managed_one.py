"""An orphan quacks like a managed process, with what a restart left it.

Liveness, the sticky ending, the stub the registry reads, the SIGTERM it
sends, the elapsed wall-clock time, and the three ways its log is read
(text, typed, and as a counted tail).
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from igni_core.core.tools.orphan_process import OrphanProcess
from igni_core.core.tools.orphan_schemas import OrphanProcStub
from igni_core.core.tools.process_log import ProcessLogStore
from igni_core.core.tools.process_store_schemas import BackgroundProcessRow

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="signals and process groups are POSIX"
)

DEAD_PID = 0x7FFFFFFE


def _orphan(tmp_path: Path, pid: int = DEAD_PID, pgid: int | None = None, started: int = 0):
    return OrphanProcess(
        pid=pid,
        cmd="serve",
        started_epoch=started,
        pgid=pgid,
        log_store=ProcessLogStore(tmp_path),
    )


def _write_log(store: ProcessLogStore, pid: int, lines: list[str]) -> None:
    path = store.path(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def sleeper():
    """A real process in its own session, as a backgrounded command is."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    yield proc
    if proc.poll() is None:
        proc.kill()
    proc.wait()


# ── Built from a row ───────────────────────────────────────────────


def test_an_orphan_is_built_from_a_persisted_row(tmp_path):
    store = ProcessLogStore(tmp_path)
    row = BackgroundProcessRow(pid=4242, cmd="npm run dev", pgid=4000, started_at=1700)

    orphan = OrphanProcess.from_row(row, store)

    assert (orphan.pid, orphan.cmd, orphan.pgid, orphan.started_epoch) == (
        4242,
        "npm run dev",
        4000,
        1700,
    )
    assert orphan._log_store is store


def test_an_orphan_carries_no_output_reader_of_its_own(tmp_path):
    orphan = _orphan(tmp_path)

    assert orphan.is_orphan is True
    assert orphan.was_backgrounded is True
    assert orphan.output == []
    assert orphan.reader_task is None
    assert orphan.read_new() == ""
    assert orphan.read_new(max_lines=1) == ""


# ── Liveness ───────────────────────────────────────────────────────


def test_a_live_orphan_has_no_return_code_and_its_stub_says_running(sleeper, tmp_path):
    orphan = _orphan(tmp_path, pid=sleeper.pid)

    assert orphan.is_running() is True
    assert orphan.returncode() is None
    assert orphan.proc == OrphanProcStub(pid=sleeper.pid, returncode=None)


def test_a_dead_orphan_reports_an_unknown_exit(tmp_path):
    orphan = _orphan(tmp_path)

    assert orphan.is_running() is False
    assert orphan.returncode() == -1
    assert orphan.proc == OrphanProcStub(pid=DEAD_PID, returncode=-1)


def test_an_orphan_seen_dead_is_never_alive_again(tmp_path, monkeypatch):
    # The pid being reused by an unrelated process must not bring the
    # orphan back to life.
    answers = iter([True, False, True, True])
    monkeypatch.setattr(OrphanProcess, "probe_alive", classmethod(lambda cls, pid: next(answers)))
    orphan = _orphan(tmp_path)

    assert orphan.is_running() is True
    assert orphan.is_running() is False
    assert orphan.is_running() is False
    assert orphan.returncode() == -1
    assert orphan.proc.returncode == -1


# ── Kill ───────────────────────────────────────────────────────────


def test_kill_terminates_the_process_group_and_the_process(sleeper, tmp_path):
    orphan = _orphan(tmp_path, pid=sleeper.pid, pgid=sleeper.pid)

    orphan.kill()

    assert sleeper.wait(timeout=10) == -15
    assert orphan.is_running() is False


def test_kill_takes_down_the_children_the_orphan_started(tmp_path):
    script = (
        "import subprocess, sys, time\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(c.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", script], start_new_session=True, stdout=subprocess.PIPE, text=True
    )
    try:
        child_pid = int(leader.stdout.readline())
        _orphan(tmp_path, pid=leader.pid, pgid=leader.pid).kill()
        leader.wait(timeout=10)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        pytest.fail("the orphan's child outlived the group kill")
    finally:
        leader.stdout.close()
        leader.kill()
        leader.wait()
        with contextlib.suppress(ProcessLookupError, UnboundLocalError):
            os.kill(child_pid, 9)


def test_kill_still_reaches_the_pid_when_its_group_is_gone(sleeper, tmp_path):
    orphan = _orphan(tmp_path, pid=sleeper.pid, pgid=DEAD_PID)

    orphan.kill()

    assert sleeper.wait(timeout=10) == -15


def test_kill_without_a_group_terminates_the_pid(sleeper, tmp_path):
    orphan = _orphan(tmp_path, pid=sleeper.pid, pgid=None)

    orphan.kill()

    assert sleeper.wait(timeout=10) == -15


def test_kill_of_a_process_that_is_gone_raises_nothing(tmp_path):
    _orphan(tmp_path, pgid=DEAD_PID).kill()


def test_kill_of_a_process_we_may_not_signal_raises_nothing(tmp_path, monkeypatch):
    def _denied(*_args):
        raise PermissionError("not ours")

    monkeypatch.setattr(os, "killpg", _denied)
    monkeypatch.setattr(os, "kill", _denied)

    _orphan(tmp_path, pid=1, pgid=1).kill()


# ── Elapsed ────────────────────────────────────────────────────────


def test_elapsed_is_wall_clock_seconds_since_the_stored_epoch(tmp_path):
    orphan = _orphan(tmp_path, started=int(time.time()) - 120)

    assert 119 <= orphan.elapsed() <= 125


# ── Reading the log ────────────────────────────────────────────────


def test_read_gives_the_last_lines_of_the_log(tmp_path):
    orphan = _orphan(tmp_path)
    _write_log(orphan._log_store, DEAD_PID, [f"line {i}" for i in range(10)])

    assert orphan.read(tail=3) == "line 7\nline 8\nline 9"
    typed = orphan.read_typed(tail=2)
    assert (typed.content, typed.is_placeholder) == ("line 8\nline 9", False)


def test_read_keeps_unicode_lines_whole(tmp_path):
    orphan = _orphan(tmp_path)
    _write_log(orphan._log_store, DEAD_PID, ["збірка ✓", "готово 🚀"])

    assert orphan.read() == "збірка ✓\nготово 🚀"


def test_an_empty_log_reads_as_a_placeholder_naming_the_start_time(tmp_path):
    started = int(time.time()) - 3600
    orphan = _orphan(tmp_path, started=started)

    typed = orphan.read_typed()

    assert typed.is_placeholder is True
    assert time.strftime("%H:%M:%S", time.localtime(started)) in typed.content
    assert "no buffered output" in typed.content
    assert orphan.read() == typed.content


def test_a_tail_counts_the_lines_before_it(tmp_path):
    orphan = _orphan(tmp_path)
    _write_log(orphan._log_store, DEAD_PID, ["aa", "bb", "cc", "dd"])

    tail = orphan.read_tail(tail=2)

    assert tail.lines == ["cc", "dd"]
    assert (tail.omitted_lines, tail.omitted_chars, tail.cut) == (2, 6, True)


def test_a_tail_of_nothing_still_counts_every_line_as_left_out(tmp_path):
    orphan = _orphan(tmp_path)
    _write_log(orphan._log_store, DEAD_PID, ["aa", "bb"])

    tail = orphan.read_tail(tail=0)

    assert tail.lines == []
    assert (tail.omitted_lines, tail.omitted_chars) == (2, 6)


def test_a_log_that_fits_is_not_cut(tmp_path):
    orphan = _orphan(tmp_path)
    _write_log(orphan._log_store, DEAD_PID, ["aa", "bb"])

    tail = orphan.read_tail(tail=5)

    assert tail.lines == ["aa", "bb"]
    assert tail.cut is False


def test_a_tail_with_no_log_is_the_placeholder_uncut(tmp_path):
    orphan = _orphan(tmp_path)

    tail = orphan.read_tail()

    assert tail.lines == [orphan.read_typed().content]
    assert tail.cut is False
