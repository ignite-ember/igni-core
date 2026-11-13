"""The chunk trace's line, its switches and where the line goes.

A line reads ``<timestamp> [chunk tx] seq= len= thinking= preview=``,
with the preview cut at 40 characters and shown with ``repr``. Only
``IGNI_CHUNK_TRACE=1`` turns it on; a blank path override means the
default; a trace that cannot be written never breaks the stream; and a
layer that keeps no log files gets the line in its log instead.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from igni_core.core.layer import Layer, layers
from igni_core.protocol import messages as msg
from igni_core.transport.chunk_trace import ChunkTrace

LINE = re.compile(
    r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \[chunk tx\] "
    r"seq=(?P<seq>\d+) len=(?P<len>\d+) thinking=(?P<thinking>True|False) preview=(?P<preview>.*)$"
)


def _written(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def test_a_line_has_a_timestamp_the_fields_and_the_preview_as_repr(tmp_path):
    path = tmp_path / "trace.log"
    ChunkTrace(path=path).record(msg.ContentDelta(text="it's\nthe é", event_seq=3))
    (line,) = _written(path)
    found = LINE.match(line)
    assert found, line
    assert found["seq"] == "3"
    assert found["len"] == str(len("it's\nthe é"))
    assert found["thinking"] == "False"
    assert found["preview"] == repr("it's\nthe é")


def test_the_preview_is_cut_at_forty_characters_and_len_is_the_whole(tmp_path):
    path = tmp_path / "trace.log"
    text = "".join(chr(ord("a") + i % 26) for i in range(100))
    ChunkTrace(path=path).record(msg.ContentDelta(text=text, is_thinking=True))
    found = LINE.match(_written(path)[0])
    assert found
    assert found["len"] == "100"
    assert found["thinking"] == "True"
    assert found["preview"] == repr(text[:40])


def test_an_empty_chunk_is_a_line_too(tmp_path):
    path = tmp_path / "trace.log"
    ChunkTrace(path=path).record(msg.ContentDelta(text=""))
    found = LINE.match(_written(path)[0])
    assert found and found["len"] == "0" and found["preview"] == "''"


def test_lines_are_appended_and_the_directory_is_made(tmp_path):
    path = tmp_path / "not" / "yet" / "trace.log"
    trace = ChunkTrace(path=path)
    trace.record(msg.ContentDelta(text="one"))
    trace.record(msg.ContentDelta(text="two"))
    assert [LINE.match(line)["preview"] for line in _written(path)] == ["'one'", "'two'"]  # type: ignore[index]
    # A new trace on the same file adds to it.
    ChunkTrace(path=path).record(msg.ContentDelta(text="three"))
    assert len(_written(path)) == 3


def test_an_enabled_trace_with_no_path_writes_nothing_and_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    trace = ChunkTrace(path=None)
    assert trace.enabled
    trace.record(msg.ContentDelta(text="nowhere"))
    assert list(tmp_path.iterdir()) == []


def test_a_trace_that_cannot_be_written_does_not_raise(tmp_path, caplog):
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    trace = ChunkTrace(path=blocker / "trace.log")
    caplog.set_level(logging.DEBUG, logger="igni_core.chunk_trace")
    trace.record(msg.ContentDelta(text="lost"))
    assert "could not be written" in caplog.text
    assert blocker.read_text() == "not a directory"


@pytest.mark.parametrize("value", ["true", "0", "", "yes", " 1", "11"])
def test_only_the_digit_one_turns_the_trace_on(monkeypatch, value):
    monkeypatch.setenv("IGNI_CHUNK_TRACE", value)
    assert not ChunkTrace.from_environment().enabled


@pytest.mark.parametrize("override", ["", "   ", "\t\n"])
def test_a_blank_path_override_means_the_default_file(monkeypatch, tmp_path, override):
    monkeypatch.setenv("IGNI_CHUNK_TRACE", "1")
    monkeypatch.setenv("IGNI_CHUNK_TRACE_LOG", override)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    ChunkTrace.from_environment().record(msg.ContentDelta(text="default"))
    assert "default" in (tmp_path / ".igni" / "chunk_trace.log").read_text()


def test_an_unset_path_override_means_the_default_file_too(monkeypatch, tmp_path):
    monkeypatch.setenv("IGNI_CHUNK_TRACE", "1")
    monkeypatch.delenv("IGNI_CHUNK_TRACE_LOG", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    ChunkTrace.from_environment().record(msg.ContentDelta(text="default"))
    assert (tmp_path / ".igni" / "chunk_trace.log").exists()


def test_a_path_override_is_trimmed(monkeypatch, tmp_path):
    target = tmp_path / "t.log"
    monkeypatch.setenv("IGNI_CHUNK_TRACE", "1")
    monkeypatch.setenv("IGNI_CHUNK_TRACE_LOG", f"  {target}  ")
    ChunkTrace.from_environment().record(msg.ContentDelta(text="trimmed"))
    assert target.exists()


class _CloudLayer(Layer):
    name = "cloud-for-the-trace-test"

    def log_files(self) -> bool:
        return False


def test_a_layer_without_log_files_gets_the_line_in_its_log_and_no_file(tmp_path, caplog):
    path = tmp_path / "trace.log"
    layers.install(_CloudLayer())
    try:
        caplog.set_level(logging.INFO, logger="igni_core.chunk_trace")
        ChunkTrace(path=path).record(msg.ContentDelta(text="cloud", event_seq=9))
    finally:
        layers.reset()
    assert not path.exists()
    (record,) = [r for r in caplog.records if r.name == "igni_core.chunk_trace"]
    assert record.levelno == logging.INFO
    assert record.getMessage() == "[chunk tx] seq=9 len=5 thinking=False preview='cloud'"
