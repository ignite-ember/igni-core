"""The debug trace of what was pushed to the coordinator.

The trace answers one question — did a sub-agent's pause reach the
coordinator? — and it has to answer it where the ordinary log pipeline
cannot: a layer that keeps no log files, a silenced test run, a cloud
producer whose log forwarder drops debug lines. So it writes straight
to a file beside the project's data, one line per push.

A trace that cannot be written must not break the run it describes, so
a failed write is logged at DEBUG and the run carries on. It is never
swallowed: the failure is visible to whoever reads the log.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from igni_core.core.layer import layers
from igni_core.core.paths import DEFAULT_DATA_DIR

#: The logger the record goes to when the layer keeps no log files.
TRACE_LOGGER = logging.getLogger("igni_core.hitl_trace")


def default_trace_path() -> Path:
    """Where the trace goes when nothing else is asked for."""
    return Path(os.path.expanduser(f"{DEFAULT_DATA_DIR}/hitl_trace.log"))


class HitlTrace:
    """Best-effort direct-write trace of the coordinator's pushes."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_trace_path()

    @property
    def path(self) -> Path:
        """The file the trace is written to."""
        return self._path

    def record(self, text: str) -> None:
        """Append one line to the trace.

        A layer that keeps no log files gets the record as a log line
        instead — there is no file to write it to. A write that fails
        is logged at DEBUG: the trace is a diagnostic, and failing to
        write one must not reach the run.
        """
        if not layers.current().log_files():
            TRACE_LOGGER.info("%s", text)
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a") as f:
                f.write(f"{time.strftime('%H:%M:%S')} pid={os.getpid()} {text}\n")
        except OSError as exc:
            TRACE_LOGGER.debug("hitl trace: cannot write %s: %s", self._path, exc)


__all__ = ["HitlTrace", "TRACE_LOGGER", "default_trace_path"]
