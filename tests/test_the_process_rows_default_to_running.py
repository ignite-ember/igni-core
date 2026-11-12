"""The process watcher's rows: an older client's row reads as running, and a
finished process keeps its exit code on the wire."""

from __future__ import annotations

from igni_core.backend.schemas_processes import (
    ProcessRow,
    ProcessTailResult,
    StopProcessResult,
)


def test_a_row_built_without_is_running_or_exit_code_reads_as_running():
    row = ProcessRow(pid=7, cmd="sleep 9", elapsed_seconds=1.5)
    assert row.model_dump() == {
        "pid": 7,
        "cmd": "sleep 9",
        "elapsed_seconds": 1.5,
        "is_running": True,
        "exit_code": None,
    }


def test_a_finished_row_stays_listed_with_its_exit_code():
    ok = ProcessRow(pid=1, cmd="true", elapsed_seconds=0.2, is_running=False, exit_code=0)
    failed = ProcessRow(pid=2, cmd="false", elapsed_seconds=0.2, is_running=False, exit_code=3)
    assert (ok.is_running, ok.exit_code) == (False, 0)
    assert (failed.is_running, failed.exit_code) == (False, 3)


def test_a_tail_and_a_stop_result_name_every_field_on_the_wire():
    tail = ProcessTailResult(pid=9, output="", is_running=False, exit_code=None)
    assert tail.model_dump() == {"pid": 9, "output": "", "is_running": False, "exit_code": None}
    stop = StopProcessResult(pid=9, killed=False, message="no such process")
    assert stop.model_dump() == {"pid": 9, "killed": False, "message": "no such process"}
