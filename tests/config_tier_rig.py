"""A log collector for the settings-tier tests.

Local handler rather than ``caplog``: an earlier test can remove pytest's
capture handler from the root logger, so a caplog assertion passes alone and
fails in the suite.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def warnings_of(logger_name: str) -> Iterator[list[str]]:
    """The warning messages ``logger_name`` logs inside the block."""
    messages: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.WARNING:
                messages.append(record.getMessage())

    logger = logging.getLogger(logger_name)
    handler = _Collect()
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        yield messages
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
