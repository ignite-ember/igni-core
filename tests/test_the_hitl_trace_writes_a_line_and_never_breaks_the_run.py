"""``HitlTrace`` (the coordinator's trace) writes a stamped line, a log record, or nothing, and never raises."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from igni_core.core.hitl.trace import TRACE_LOGGER, HitlTrace, default_trace_path
from igni_core.core.layer import Layer, layers
from igni_core.core.paths import DEFAULT_DATA_DIR


class NoLogFiles(Layer):
    def log_files(self) -> bool:
        return False


@pytest.fixture
def no_log_files() -> Iterator[None]:
    layers.install(NoLogFiles())
    try:
        yield
    finally:
        layers.reset()


def test_the_default_path_is_the_trace_log_in_the_expanded_data_directory():
    path = default_trace_path()

    assert path == Path(os.path.expanduser(DEFAULT_DATA_DIR)) / "hitl_trace.log"
    assert "~" not in str(path)


def test_the_path_is_the_one_given_or_the_default(tmp_path: Path):
    assert HitlTrace(tmp_path / "t.log").path == tmp_path / "t.log"
    assert HitlTrace().path == default_trace_path()


def test_each_record_is_one_stamped_line_and_the_directories_are_made(tmp_path: Path):
    path = tmp_path / "a" / "b" / "trace.log"
    trace = HitlTrace(path)

    trace.record("push run=r1")
    trace.record("héllo ✓")

    stamp = rf"^\d\d:\d\d:\d\d pid={os.getpid()} "
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [re.sub(stamp, "", line) for line in lines] == ["push run=r1", "héllo ✓"]
    assert all(re.match(stamp, line) for line in lines)


def test_a_layer_without_log_files_gets_an_info_record_and_no_file(
    tmp_path: Path, no_log_files, caplog: pytest.LogCaptureFixture
):
    path = tmp_path / "trace.log"

    with caplog.at_level(logging.INFO, logger=TRACE_LOGGER.name):
        HitlTrace(path).record("push run=r1")

    assert TRACE_LOGGER.name == "igni_core.hitl_trace"
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [(logging.INFO, "push run=r1")]
    assert not path.exists()
    assert not path.parent.exists() or list(path.parent.iterdir()) == []


def test_a_write_that_fails_is_logged_at_debug_and_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    blocked = tmp_path / "is-a-directory"
    blocked.mkdir()

    with caplog.at_level(logging.DEBUG, logger=TRACE_LOGGER.name):
        HitlTrace(blocked).record("lost")

    assert [r.levelno for r in caplog.records] == [logging.DEBUG]
    message = caplog.records[0].getMessage()
    assert message.startswith("hitl trace: cannot write ") and str(blocked) in message
