"""A process that ended is rebuilt from its persisted row and answers the
small surface the registry and the watcher read — without pretending there is
a process behind it.

``read`` used to call a method the log store does not have; the
``AttributeError`` was swallowed as "no stored log", so a finished process
always read back empty. A recorded exit code is shown as it is, and an
unrecorded one stays unknown rather than being made up.
"""

from __future__ import annotations

import pytest

from igni_core.core.tools.finished_process import FinishedProcess
from igni_core.core.tools.process_log import ProcessLogStore
from igni_core.core.tools.process_store_schemas import BackgroundProcessRow


def _row(**kwargs) -> BackgroundProcessRow:
    fields = {"pid": 4242, "cmd": "make build", "started_at": 1000, **kwargs}
    return BackgroundProcessRow(**fields)


def _store_with(tmp_path, pid: int, lines: list[str]) -> ProcessLogStore:
    store = ProcessLogStore(project_dir=tmp_path)
    handle = store.open(pid)
    assert handle is not None
    handle.write("".join(f"{line}\n" for line in lines))
    handle.close()
    return store


def test_the_row_s_fields_become_the_process_s_surface():
    process = FinishedProcess.from_row(_row(finished_at=1007, exit_code=2))

    assert process.cmd == "make build"
    assert process.started_epoch == 1000
    assert process.proc.pid == 4242
    assert process.proc.returncode == 2
    assert process.returncode() == 2
    assert process.elapsed() == 7.0


def test_it_is_never_running_and_a_restored_orphan_by_kind():
    process = FinishedProcess.from_row(_row(finished_at=1001, exit_code=0))

    assert process.is_running() is False
    assert FinishedProcess.is_orphan is True
    assert process.reader_task is None
    assert process.kill() is None
    assert process.read_new() == ""
    assert process.read_new(max_lines=1) == ""


@pytest.mark.parametrize("code", [0, 1, -9])
def test_a_recorded_exit_code_is_reported_as_it_was(code):
    assert FinishedProcess.from_row(_row(finished_at=1001, exit_code=code)).returncode() == code


def test_an_ending_the_backend_never_saw_has_no_invented_exit_code():
    process = FinishedProcess.from_row(_row(finished_at=None, exit_code=None))

    assert process.returncode() is None
    assert process.proc.returncode is None


def test_without_a_finish_time_it_ran_for_zero_seconds():
    assert FinishedProcess.from_row(_row(finished_at=None)).elapsed() == 0.0


def test_a_finish_before_the_start_never_gives_a_negative_duration():
    assert FinishedProcess.from_row(_row(started_at=1000, finished_at=990)).elapsed() == 0.0


def test_it_has_no_room_for_ad_hoc_attributes():
    process = FinishedProcess.from_row(_row())

    with pytest.raises(AttributeError):
        process.surprise = 1  # type: ignore[attr-defined]


def test_the_stored_log_is_read_back_not_empty(tmp_path):
    store = _store_with(tmp_path, 4242, ["one", "two", "three"])
    process = FinishedProcess.from_row(_row(finished_at=1001), store)

    assert process.read() == "one\ntwo\nthree"


def test_read_gives_the_last_lines_asked_for(tmp_path):
    store = _store_with(tmp_path, 4242, [f"line {n}" for n in range(1, 11)])
    process = FinishedProcess.from_row(_row(finished_at=1001), store)

    assert process.read(tail=3) == "line 8\nline 9\nline 10"
    assert process.read(tail=100).count("\n") == 9
    assert process.read(tail=0) == ""


def test_read_tail_counts_what_it_cut(tmp_path):
    store = _store_with(tmp_path, 4242, ["a", "bb", "ccc", "dddd"])
    process = FinishedProcess.from_row(_row(finished_at=1001), store)

    tail = process.read_tail(2)

    assert tail.lines == ["ccc", "dddd"]
    assert tail.omitted_lines == 2
    assert tail.omitted_chars == len("a") + 1 + len("bb") + 1


def test_a_log_that_is_missing_reads_as_empty_not_as_an_error(tmp_path):
    process = FinishedProcess.from_row(
        _row(finished_at=1001), ProcessLogStore(project_dir=tmp_path)
    )

    assert process.read() == ""
    assert process.read_tail().lines == []


def test_with_no_log_store_it_reads_as_empty():
    process = FinishedProcess.from_row(_row(finished_at=1001), None)

    assert process.read() == ""
    tail = process.read_tail()
    assert (tail.lines, tail.omitted_lines, tail.omitted_chars) == ([], 0, 0)


def test_a_log_store_that_raises_reads_as_empty(tmp_path):
    class Broken(ProcessLogStore):
        def lines(self, pid: int) -> list[str]:
            raise OSError("disk gone")

    process = FinishedProcess.from_row(_row(finished_at=1001), Broken(project_dir=tmp_path))

    assert process.read() == ""
    assert process.read_tail().lines == []
