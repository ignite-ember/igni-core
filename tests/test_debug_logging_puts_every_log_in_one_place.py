"""``igni --debug`` sends every log, including the bodies, to one place.

``DebugLogging.enable`` swaps the root logger's handlers for one, and hands
that same handler to the three loggers that never propagate to root (the
model-call log and the HTTP transports), so a debug run captures their bodies
without reopening the path that put them on stderr.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from igni_core.cli.invocation import CliInvocation
from igni_core.cli.logging import DebugLogging
from igni_core.cli.options import CliOptions
from igni_core.core.layer import Layer, layers

NAMED = ("igni_core.llm_calls", "httpx", "httpcore")


@pytest.fixture(autouse=True)
def _untouched_logging() -> Iterator[None]:
    root = logging.getLogger()
    saved_root = (list(root.handlers), root.level)
    saved_named = {
        n: (list(logging.getLogger(n).handlers), logging.getLogger(n).level)
        for n in (*NAMED, "igni_core")
    }
    yield
    for handler in root.handlers:
        handler.close()
    root.handlers[:] = saved_root[0]
    root.setLevel(saved_root[1])
    for name, (handlers, level) in saved_named.items():
        logging.getLogger(name).handlers[:] = handlers
        logging.getLogger(name).setLevel(level)
    layers.reset()


def test_a_record_lands_in_the_returned_file_in_the_debug_format(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "debug.log"
    assert DebugLogging.enable(path) == path
    logging.getLogger("igni_core.some.module").debug("hello debug")
    for handler in logging.getLogger().handlers:
        handler.flush()
    line = path.read_text().strip()
    assert line.endswith("igni_core.some.module DEBUG hello debug")
    assert line[:4].isdigit()  # asctime comes first


def test_the_root_has_exactly_one_handler_and_debug_levels(tmp_path: Path) -> None:
    logging.getLogger().addHandler(logging.NullHandler())
    DebugLogging.enable(tmp_path / "a.log")
    DebugLogging.enable(tmp_path / "b.log")
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert root.level == logging.DEBUG
    assert logging.getLogger("igni_core").level == logging.DEBUG


def test_the_file_rotates_at_ten_megabytes_keeping_two_backups(tmp_path: Path) -> None:
    DebugLogging.enable(tmp_path / "debug.log")
    (handler,) = logging.getLogger().handlers
    assert isinstance(handler, RotatingFileHandler)
    assert (handler.maxBytes, handler.backupCount) == (10_000_000, 2)


def test_the_non_propagating_loggers_write_to_the_same_file(tmp_path: Path) -> None:
    path = tmp_path / "debug.log"
    DebugLogging.enable(path)
    for name in NAMED:
        logging.getLogger(name).propagate = False
        logging.getLogger(name).setLevel(logging.DEBUG)
        logging.getLogger(name).debug("body of %s", name)
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = path.read_text()
    for name in NAMED:
        assert text.count(f"body of {name}") == 1


def test_a_layer_without_log_files_gets_its_own_handler_and_no_file(tmp_path: Path) -> None:
    records: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    collector = Collect()

    class NoFiles(Layer):
        def log_files(self) -> bool:
            return False

        def log_handler(self) -> logging.Handler:
            return collector

    layers.install(NoFiles())
    path = tmp_path / "never" / "debug.log"
    DebugLogging.enable(path)
    assert logging.getLogger().handlers == [collector]
    logging.getLogger("httpx").debug("through the layer")
    assert [r.getMessage() for r in records] == ["through the layer"]
    assert not path.parent.exists()


def test_igni_debug_prints_where_the_log_is_and_says_nothing_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(DebugLogging, "DEFAULT_PATH", tmp_path / "debug.log")
    CliInvocation(CliOptions(), None, None).enable_debug_logging()  # type: ignore[arg-type]
    assert capsys.readouterr().out == ""

    CliInvocation(CliOptions(debug=True), None, None).enable_debug_logging()  # type: ignore[arg-type]
    assert f"Debug logging enabled → {tmp_path / 'debug.log'}" in capsys.readouterr().out


def test_a_transport_record_before_any_model_call_is_written_once(tmp_path: Path) -> None:
    """``--debug`` hands the transport loggers its handler directly; were they still
    propagating, root's copy of the same handler wrote every line a second time."""
    path = tmp_path / "debug.log"
    DebugLogging.enable(path)
    logging.getLogger("httpx").debug("one request")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert path.read_text().count("one request") == 1
    assert not any(logging.getLogger(n).propagate for n in NAMED)
