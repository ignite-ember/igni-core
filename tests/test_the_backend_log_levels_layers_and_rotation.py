"""The backend log beyond the always-on default: levels, layers, rotation.

``test_backend_logging.py`` pins that a log is kept without being asked.
These pin the dial (``--debug`` and ``IGNI_DEBUG_LOG``), what a second
call changes, where the log goes by default, the layer that keeps no log
file, and that the file really rolls over at its size limit.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from igni_core.backend import logging_setup
from igni_core.core.layer import Layer, layers


@pytest.fixture(autouse=True)
def _clean_package_logger(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The package logger is process-wide: put it back as it was."""
    monkeypatch.delenv("IGNI_DEBUG_LOG", raising=False)
    monkeypatch.delenv("IGNI_DEBUG_LOG_PATH", raising=False)
    pkg = logging.getLogger("igni_core")
    handlers, level = list(pkg.handlers), pkg.level
    try:
        yield
    finally:
        for handler in list(pkg.handlers):
            if handler not in handlers:
                handler.close()
                pkg.removeHandler(handler)
        pkg.setLevel(level)
        layers.reset()


def _marked_handlers() -> list[logging.Handler]:
    return [
        h
        for h in logging.getLogger("igni_core").handlers
        if getattr(h, logging_setup._HANDLER_MARK, False)
    ]


def test_the_debug_flag_keeps_debug_records(tmp_path: Path) -> None:
    path = tmp_path / "backend.log"

    assert logging_setup.configure_logging(debug_flag=True, path=path) == path
    logging.getLogger("igni_core.chatty").debug("fine detail")

    assert "fine detail" in path.read_text()


def test_a_second_call_with_another_level_changes_the_level_not_the_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "backend.log"
    logging_setup.configure_logging(path=path)
    [handler] = _marked_handlers()
    log = logging.getLogger("igni_core.dial")
    log.debug("before the dial")

    monkeypatch.setenv("IGNI_DEBUG_LOG", "1")
    assert logging_setup.configure_logging(path=path) == path
    log.debug("after the dial")

    assert _marked_handlers() == [handler]
    assert handler.level == logging.DEBUG
    assert logging.getLogger("igni_core").level == logging.DEBUG
    text = path.read_text()
    assert "before the dial" not in text and "after the dial" in text


def test_an_env_value_that_is_empty_is_off_and_an_empty_path_means_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IGNI_DEBUG_LOG", "")
    monkeypatch.setenv("IGNI_DEBUG_LOG_PATH", "")

    assert logging_setup.resolve_level() == logging.INFO
    assert logging_setup.resolve_log_path() == Path.home() / ".igni" / "logs" / "backend.log"


def test_the_default_log_lives_under_the_config_dir_of_the_home() -> None:
    assert logging_setup.resolve_log_path() == Path.home() / ".igni" / "logs" / "backend.log"


def test_the_env_path_is_where_an_unnamed_log_goes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    where = tmp_path / "chosen" / "support.log"
    monkeypatch.setenv("IGNI_DEBUG_LOG_PATH", str(where))

    assert logging_setup.configure_logging() == where
    logging.getLogger("igni_core.support").warning("for the support request")

    assert "for the support request" in where.read_text()


def test_a_record_line_carries_time_logger_level_and_message(tmp_path: Path) -> None:
    path = tmp_path / "backend.log"
    logging_setup.configure_logging(path=path)

    logging.getLogger("igni_core.format").warning("naïve — ünï")

    [line] = path.read_text(encoding="utf-8").splitlines()
    assert line.endswith(" igni_core.format WARNING naïve — ünï")


def test_the_file_rolls_over_at_its_size_limit_and_keeps_its_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logging_setup, "_MAX_BYTES", 300)
    monkeypatch.setattr(logging_setup, "_BACKUP_COUNT", 2)
    path = tmp_path / "backend.log"
    logging_setup.configure_logging(path=path)

    for i in range(60):
        logging.getLogger("igni_core.roll").warning("line %02d %s", i, "x" * 40)

    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["backend.log", "backend.log.1", "backend.log.2"]
    assert path.stat().st_size <= 300 + 120
    assert "line 59" in path.read_text()


class _NoFilesLayer(Layer):
    """The cloud's choice: no log files, one stream."""

    def __init__(self) -> None:
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)

    def log_files(self) -> bool:
        return False

    def log_handler(self) -> logging.Handler:
        return self.handler


def test_a_layer_that_keeps_no_log_file_gets_its_stream_once_and_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer = _NoFilesLayer()
    layers.install(layer)
    where = tmp_path / "never.log"
    monkeypatch.setenv("IGNI_DEBUG_LOG_PATH", str(where))
    pkg = logging.getLogger("igni_core")
    pkg.setLevel(logging.WARNING)

    assert logging_setup.configure_logging() is None
    assert logging_setup.configure_logging() is None

    assert pkg.handlers.count(layer.handler) == 1
    assert _marked_handlers() == []
    assert not where.exists()
    assert pkg.level == logging.INFO
    logging.getLogger("igni_core.cloud").info("to the stream")
    assert "to the stream" in layer.stream.getvalue()


@pytest.mark.parametrize(
    ("before", "debug", "after"),
    [
        (logging.NOTSET, False, logging.INFO),
        (logging.DEBUG, False, logging.DEBUG),
        (logging.ERROR, True, logging.DEBUG),
    ],
)
def test_a_layer_without_files_only_ever_lowers_the_package_level(
    before: int, debug: bool, after: int
) -> None:
    layers.install(_NoFilesLayer())
    pkg = logging.getLogger("igni_core")
    pkg.setLevel(before)

    logging_setup.configure_logging(debug_flag=debug)

    assert pkg.level == after
