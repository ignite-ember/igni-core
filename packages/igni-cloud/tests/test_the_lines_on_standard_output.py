"""The cloud's log lines: one JSON object per record, on standard output.

``docs/CLOUD.md`` §9. The node collects the VM's standard output, so a
line is the whole of what is known about a record: when it was logged,
how loudly, by whom, what it said, the structured fields it carried and
the exception it raised. The writer is a thread of its own, and after a
restore it waits for the first answer.
"""

from __future__ import annotations

import io
import json
import logging
import subprocess
import sys

import pytest
from igni_cloud.logs import JsonLineFormatter, LogFields, QueuedRecords, StdoutLog


def _record(message: str = "hello", created: float | None = None) -> logging.LogRecord:
    record = logging.LogRecord(
        "igni_cloud.tests", logging.INFO, "somewhere", 1, message, None, None
    )
    if created is not None:
        record.created = created
    return record


def test_a_line_carries_the_records_own_time():
    line = json.loads(JsonLineFormatter().format(_record(created=1_700_000_000.0)))
    assert line["ts"] == "2023-11-14T22:13:20+00:00"


def test_a_record_that_carries_a_plain_dict_of_extras_contributes_no_field():
    record = _record()
    record.fields = {"loose": "value"}  # not the typed fields
    line = json.loads(JsonLineFormatter().format(record))
    assert set(line) == {"ts", "level", "logger", "message"}


def test_each_field_section_is_a_key_of_the_line():
    record = _record()
    record.fields = LogFields(sections={"audit": {"tool": "run_shell_command"}})
    line = json.loads(JsonLineFormatter().format(record))
    assert line["audit"] == {"tool": "run_shell_command"}


def test_a_record_with_an_exception_carries_it_on_the_line(
    capsys: pytest.CaptureFixture[str],
):
    logs = StdoutLog()
    log = logs.attach("igni_cloud.tests.exception")
    try:
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("it broke %s", "badly")
        logs.flush()
        line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert line["message"] == "it broke badly"
        assert line["level"] == "ERROR"
        assert "ValueError: boom" in line["exception"]
        assert "Traceback" in line["exception"]
    finally:
        log.removeHandler(logs.handler())
        logs.reset()


def test_more_than_a_thousand_records_held_are_written_before_the_held_ones(
    capsys: pytest.CaptureFixture[str],
):
    logs = StdoutLog()
    log = logs.attach("igni_cloud.tests.overflow")
    try:
        logs.hold()
        for index in range(QueuedRecords.MAX_HELD + 1):
            log.info("held %d", index)
        logs.flush()
        written = [json.loads(x)["message"] for x in capsys.readouterr().out.splitlines()]
        assert written == [f"held {QueuedRecords.MAX_HELD}"], "the surplus is written at once"
        logs.release()
        logs.flush()
        written = [json.loads(x)["message"] for x in capsys.readouterr().out.splitlines()]
        assert written == [f"held {index}" for index in range(QueuedRecords.MAX_HELD)], (
            "the held ones keep their order, behind the surplus"
        )
    finally:
        log.removeHandler(logs.handler())
        logs.reset()


def test_attaching_leaves_a_verbose_logger_alone():
    logs = StdoutLog()
    verbose = logging.getLogger("igni_cloud.tests.verbose")
    verbose.setLevel(logging.DEBUG)
    unset = logging.getLogger("igni_cloud.tests.unset")
    unset.setLevel(logging.NOTSET)
    try:
        logs.attach("igni_cloud.tests.verbose")
        logs.attach("igni_cloud.tests.unset")
        assert verbose.level == logging.DEBUG, "more verbose than INFO stays"
        assert unset.level == logging.INFO
    finally:
        verbose.handlers.clear()
        unset.handlers.clear()
        logs.reset()


def test_attaching_the_same_logger_twice_adds_one_handler():
    logs = StdoutLog()
    target = logging.getLogger("igni_cloud.tests.twice")
    try:
        logs.attach("igni_cloud.tests.twice")
        logs.attach("igni_cloud.tests.twice")
        assert target.handlers.count(logs.handler()) == 1
    finally:
        target.handlers.clear()
        logs.reset()


def test_nothing_is_held_before_a_handler_exists(capsys: pytest.CaptureFixture[str]):
    logs = StdoutLog()
    logs.hold()
    logs.flush()
    logs.release()
    log = logs.attach("igni_cloud.tests.late")
    try:
        log.info("written at once")
        logs.flush()
        assert "written at once" in capsys.readouterr().out
    finally:
        log.removeHandler(logs.handler())
        logs.reset()


def test_a_reset_forgets_the_handler_and_the_next_one_writes_to_the_current_stdout(
    capsys: pytest.CaptureFixture[str],
):
    logs = StdoutLog()
    log = logs.attach("igni_cloud.tests.reset")
    log.info("before the reset")
    logs.reset()
    written = io.StringIO()
    seen = sys.stdout
    sys.stdout = written
    try:
        again = logs.attach("igni_cloud.tests.reset")
        again.info("after the reset")
        logs.flush()
    finally:
        sys.stdout = seen
    assert "after the reset" in written.getvalue(), "the current standard output"
    assert "before the reset" in capsys.readouterr().out


def test_a_reset_twice_is_a_no_op():
    logs = StdoutLog()
    logs.attach("igni_cloud.tests.reset")
    logs.reset()
    logs.reset()


def test_the_last_lines_are_written_before_the_process_ends():
    """A record held when the process ends is written by the exit hook."""
    script = (
        "from igni_cloud.logs import StdoutLog\n"
        "logs = StdoutLog()\n"
        "log = logs.attach('igni_cloud.tests.exit')\n"
        "logs.hold()\n"
        "log.info('the last line')\n"
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert "the last line" in out.stdout, out.stderr


def test_the_exit_hook_is_registered_with_the_first_handler():
    import atexit

    registered: list[object] = []
    unregistered: list[object] = []
    original_register, original_unregister = atexit.register, atexit.unregister
    atexit.register = registered.append  # type: ignore[assignment]
    atexit.unregister = unregistered.append  # type: ignore[assignment]
    try:
        logs = StdoutLog()
        logs.handler()
        logs.handler()
        assert len(registered) == 1, "one hook, however many handlers"
        logs.reset()
        assert unregistered == [logs.reset], "the hook goes when the writer is forgotten"
    finally:
        atexit.register = original_register  # type: ignore[assignment]
        atexit.unregister = original_unregister  # type: ignore[assignment]
        logs.reset()


def test_the_writer_is_the_processes_one_and_the_handler_is_made_once():
    logs = StdoutLog()
    try:
        assert logs.handler() is logs.handler()
    finally:
        logs.reset()


def test_a_unicode_message_stays_on_one_line(capsys: pytest.CaptureFixture[str]):
    logs = StdoutLog()
    log = logs.attach("igni_cloud.tests.unicode")
    try:
        log.info("héllo wörld")
        logs.flush()
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["message"] == "héllo wörld"
    finally:
        log.removeHandler(logs.handler())
        logs.reset()


def test_a_line_says_only_what_was_logged(capsys: pytest.CaptureFixture[str]):
    """The line says what happened, not where the file was."""
    logs = StdoutLog()
    log = logs.attach("igni_cloud.tests.no_path")
    try:
        log.info("just the message")
        logs.flush()
        line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert set(line) == {"ts", "level", "logger", "message"}
        assert line["logger"] == "igni_cloud.tests.no_path"
    finally:
        log.removeHandler(logs.handler())
        logs.reset()
