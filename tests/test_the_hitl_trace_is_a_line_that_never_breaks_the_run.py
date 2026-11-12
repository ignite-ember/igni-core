"""The HITL trace writes one line per event, or a log record, and never raises.

The pause multiplexer traces what it does so a stuck approval can be
followed afterwards. A trace is a debugging aid: when it is off it
writes nothing, where igni keeps no log files it is a log record
instead of a file, and a write that fails is never the run's failure.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from igni_core.backend import hitl_tracer
from igni_core.backend.hitl_tracer import HITLTracer
from igni_core.core.layer import Layer, layers
from igni_core.core.paths import DEFAULT_DATA_DIR


class NoLogFiles(Layer):
    """A layer whose logs are records, as in the cloud."""

    def log_files(self) -> bool:
        return False


@pytest.fixture
def no_log_files() -> Iterator[None]:
    layers.install(NoLogFiles())
    try:
        yield
    finally:
        layers.reset()


def test_a_trace_appends_one_stamped_line_per_call_and_makes_the_directory(tmp_path: Path):
    path = tmp_path / "not" / "yet" / "trace.log"
    tracer = HITLTracer(path=path)

    tracer.trace("_stream_mux: starting")
    tracer.trace("drain woke, 2 entries")

    lines = path.read_text().splitlines()
    stamp = rf"^\d\d:\d\d:\d\d pid={os.getpid()} "
    assert [re.sub(stamp, "", line) for line in lines] == [
        "_stream_mux: starting",
        "drain woke, 2 entries",
    ]
    assert all(re.match(stamp, line) for line in lines)


def test_a_disabled_tracer_writes_nothing(tmp_path: Path):
    path = tmp_path / "trace.log"

    HITLTracer(path=path, enabled=False).trace("never written")

    assert not path.exists() and not path.parent.joinpath("trace.log").exists()


def test_a_multiline_or_unicode_text_is_written_as_given(tmp_path: Path):
    path = tmp_path / "trace.log"

    HITLTracer(path=path).trace("перший\nдругий ✓")

    assert path.read_text().endswith(" перший\nдругий ✓\n")


def test_where_igni_keeps_no_log_files_the_text_is_a_record_and_no_file_is_written(
    tmp_path: Path, no_log_files: None, caplog: pytest.LogCaptureFixture
):
    path = tmp_path / "trace.log"
    caplog.set_level(logging.INFO, logger="igni_core.hitl_trace")

    HITLTracer(path=path).trace("a pause was relayed")

    assert [(r.name, r.getMessage()) for r in caplog.records] == [
        ("igni_core.hitl_trace", "a pause was relayed")
    ]
    assert not path.exists()


def test_a_disabled_tracer_logs_no_record_either(
    tmp_path: Path, no_log_files: None, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.INFO, logger="igni_core.hitl_trace")

    HITLTracer(path=tmp_path / "trace.log", enabled=False).trace("silent")

    assert caplog.records == []


def test_a_write_that_fails_is_swallowed(tmp_path: Path):
    a_directory = tmp_path / "trace.log"
    a_directory.mkdir()
    under_a_file = tmp_path / "plain-file"
    under_a_file.write_text("x")

    HITLTracer(path=a_directory).trace("cannot be written")
    HITLTracer(path=under_a_file / "trace.log").trace("cannot be created")

    assert a_directory.is_dir() and under_a_file.read_text() == "x"


def test_without_a_path_the_trace_goes_to_hitl_trace_log_in_the_igni_data_dir():
    default = hitl_tracer._DEFAULT_PATH

    assert default.is_absolute()
    assert default.name == "hitl_trace.log"
    assert default.parent.name == Path(DEFAULT_DATA_DIR).name


def test_settings_do_not_move_it_and_it_is_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    default = tmp_path / "data" / "hitl_trace.log"
    monkeypatch.setattr(hitl_tracer, "_DEFAULT_PATH", default)

    HITLTracer.from_settings(object()).trace("from settings")
    HITLTracer().trace("no path given")

    lines = default.read_text().splitlines()
    assert [line.split(" ", 2)[2] for line in lines] == ["from settings", "no path given"]
