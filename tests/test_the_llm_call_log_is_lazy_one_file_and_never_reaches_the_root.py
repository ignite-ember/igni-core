"""``LlmCallLogger``: one line per model call, in one file, nowhere else.

Nothing is created until the first call; the handler is installed once
however many loggers share the process; the transport loggers (httpx,
httpcore) write to the same file at DEBUG; and none of them propagates to
the root logger, whose handler may be a pipe nobody drains. A layer that
keeps no log files gets its own handler instead of a file; a log directory
that cannot be written costs the log, not the call.
"""

from __future__ import annotations

import io
import logging
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from igni_core.core.config.llm_call_logger import LlmCallLogger
from igni_core.core.layer import Layer, layers

_NAMES = ("igni_core.llm_calls", "httpx", "httpcore")


@pytest.fixture(autouse=True)
def _pristine_loggers() -> Iterator[None]:
    """The three loggers are process-wide: put them back as they were."""
    saved = {
        n: (list(logging.getLogger(n).handlers), logging.getLogger(n).level)
        + (logging.getLogger(n).propagate,)
        for n in _NAMES
    }
    for n in _NAMES:
        logging.getLogger(n).handlers = []
    layers.reset()
    try:
        yield
    finally:
        for n, (handlers, level, propagate) in saved.items():
            log = logging.getLogger(n)
            for h in log.handlers:
                h.close()
            log.handlers = handlers
            log.setLevel(level)
            log.propagate = propagate
        layers.reset()


class _Chain:
    def __init__(self) -> None:
        self.depths: list[int] = []

    def format_caller_chain(self, depth: int = 2, max_frames: int = 4) -> str:
        self.depths.append(depth)
        return "a.py:1(f) <- b.py:2(g)"


def _call(logger: LlmCallLogger, **over) -> None:
    logger.log_call(
        **{
            "method": "invoke",
            "model_id": "m-1",
            "n_messages": 3,
            "stream": False,
            "url": "https://llm.example/v1",
            **over,
        }
    )


def _flush() -> None:
    for n in _NAMES:
        for h in logging.getLogger(n).handlers:
            h.flush()


def test_constructing_one_creates_nothing(tmp_path: Path) -> None:
    where = tmp_path / "logs"

    LlmCallLogger(log_dir=where, caller_inspector=_Chain())  # type: ignore[arg-type]

    assert not where.exists()
    assert all(not logging.getLogger(n).handlers for n in _NAMES)


def test_a_call_is_one_line_in_llm_calls_log_in_the_missing_nested_directory(
    tmp_path: Path,
) -> None:
    chain = _Chain()
    logger = LlmCallLogger(log_dir=tmp_path / "a" / "b", caller_inspector=chain)  # type: ignore[arg-type]

    _call(logger, method="ainvoke_stream", stream=True, caller_depth=5)
    _flush()

    lines = (tmp_path / "a" / "b" / "llm_calls.log").read_text().splitlines()
    assert len(lines) == 1
    assert re.fullmatch(
        r"\d\d:\d\d:\d\d LLM call: ainvoke_stream \| model=m-1 \| messages=3 \| stream=True"
        r" \| url=https://llm\.example/v1 \| caller=a\.py:1\(f\) <- b\.py:2\(g\)",
        lines[0],
    ), lines[0]
    assert chain.depths == [5]


def test_a_directory_written_with_a_tilde_is_the_home_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    _call(LlmCallLogger(log_dir="~/logs", caller_inspector=_Chain()))  # type: ignore[arg-type]
    _flush()

    assert (tmp_path / "logs" / "llm_calls.log").exists()


def test_the_handler_is_installed_once_and_a_second_instance_adopts_it(tmp_path: Path) -> None:
    first = LlmCallLogger(log_dir=tmp_path / "one", caller_inspector=_Chain())  # type: ignore[arg-type]
    second = LlmCallLogger(log_dir=tmp_path / "two", caller_inspector=_Chain())  # type: ignore[arg-type]

    first.ensure_configured()
    first.ensure_configured()
    _call(first)
    _call(second)
    _flush()

    for name in _NAMES:
        assert len(logging.getLogger(name).handlers) == 1
    assert len((tmp_path / "one" / "llm_calls.log").read_text().splitlines()) == 2
    assert not (tmp_path / "two").exists()


def test_the_transport_loggers_share_the_file_at_debug_and_nothing_propagates(
    tmp_path: Path,
) -> None:
    logger = LlmCallLogger(log_dir=tmp_path, caller_inspector=_Chain())  # type: ignore[arg-type]
    root_stream = io.StringIO()
    root_handler = logging.StreamHandler(root_stream)
    root_handler.setLevel(logging.NOTSET)
    logging.getLogger().addHandler(root_handler)
    try:
        logger.ensure_configured()
        logging.getLogger("httpcore.connection").debug("connect_tcp started")
        logging.getLogger("httpx").debug("HTTP Request: POST")
        _call(logger)
        _flush()
    finally:
        logging.getLogger().removeHandler(root_handler)

    shared = logging.getLogger("igni_core.llm_calls").handlers[0]
    assert logging.getLogger("httpx").handlers == [shared]
    assert logging.getLogger("httpcore").handlers == [shared]
    assert logging.getLogger("httpx").level == logging.DEBUG
    assert logging.getLogger("httpcore").level == logging.DEBUG
    assert logging.getLogger("igni_core.llm_calls").level == logging.INFO
    assert all(not logging.getLogger(n).propagate for n in _NAMES)
    text = (tmp_path / "llm_calls.log").read_text()
    assert "connect_tcp started" in text
    assert "HTTP Request: POST" in text
    assert root_stream.getvalue() == ""


class _StreamLayer(Layer):
    def __init__(self) -> None:
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)

    def log_files(self) -> bool:
        return False

    def log_handler(self) -> logging.Handler:
        return self.handler


def test_a_layer_that_keeps_no_log_files_gets_the_lines_on_its_handler(tmp_path: Path) -> None:
    layer = _StreamLayer()
    layers.install(layer)
    where = tmp_path / "never"

    _call(LlmCallLogger(log_dir=where, caller_inspector=_Chain()))  # type: ignore[arg-type]

    assert "LLM call: invoke | model=m-1" in layer.stream.getvalue()
    assert not where.exists()
    assert logging.getLogger("httpx").handlers == [layer.handler]


def test_a_log_directory_that_cannot_be_written_costs_the_log_not_the_call(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    logger = LlmCallLogger(log_dir=blocker / "logs", caller_inspector=_Chain())  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING):
        _call(logger)
        _call(logger)

    assert "not writable" in caplog.text
    assert all(not logging.getLogger(n).propagate for n in _NAMES)
    assert len(logging.getLogger("igni_core.llm_calls").handlers) == 1
