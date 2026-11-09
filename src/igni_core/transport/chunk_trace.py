"""The chunk trace — one line per chunk the transport broadcasts.

An operator's aid: with ``IGNI_CHUNK_TRACE=1`` the WebSocket transport
writes a line for every chunk it broadcasts, so a person can tell
whether the backend is really streaming every chunk or coalescing or
dropping them under back-pressure. ``IGNI_CHUNK_TRACE_LOG`` says where
the file is; otherwise it is ``~/.igni/chunk_trace.log``.

The trace writes to its own file instead of going through ``logging``:
a trace that stops when the process's logging is reconfigured is no
trace at all. :class:`igni_core.backend.hitl_tracer.HITLTracer` — the
HITL trace — writes its own file for the same reason.

The transport owns one instance and hands it every chunk it
broadcasts; a test hands it one that writes into a temporary
directory.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from igni_core.core.layer import layers
from igni_core.core.paths import CONFIG_DIR
from igni_core.protocol.schemas.be_events import ContentDelta

__all__ = ["CHUNK_TRACE_ENV", "CHUNK_TRACE_LOG_ENV", "TRACE_FILE_NAME", "ChunkTrace"]

#: The environment variable that turns the trace on.
CHUNK_TRACE_ENV = "IGNI_CHUNK_TRACE"
#: The environment variable that says where the trace file is.
CHUNK_TRACE_LOG_ENV = "IGNI_CHUNK_TRACE_LOG"

#: The trace file's name under the config directory.
TRACE_FILE_NAME = "chunk_trace.log"

#: The trace's own logger — where a layer that keeps no log files (the
#: cloud's) sends the trace.
logger = logging.getLogger("igni_core.chunk_trace")


class ChunkTrace:
    """One line per chunk, written straight to the trace file.

    Every line is appended and flushed before the call returns, so a
    ``tail -f`` shows a chunk the moment it leaves the backend.
    """

    def __init__(self, path: Path | None = None, enabled: bool = True) -> None:
        """A trace writing to ``path``; ``enabled=False`` writes nothing."""
        self._path = path
        self._enabled = enabled

    @classmethod
    def from_environment(cls) -> ChunkTrace:
        """The trace the environment asks for.

        Off unless ``IGNI_CHUNK_TRACE=1``. ``IGNI_CHUNK_TRACE_LOG`` says
        where the file is; otherwise it is ``~/.igni/chunk_trace.log``.
        """
        if os.environ.get(CHUNK_TRACE_ENV) != "1":
            return cls(enabled=False)
        override = (os.environ.get(CHUNK_TRACE_LOG_ENV) or "").strip()
        path = Path(override) if override else Path.home() / CONFIG_DIR / TRACE_FILE_NAME
        return cls(path=path)

    @property
    def enabled(self) -> bool:
        """Whether this trace writes anything."""
        return self._enabled

    def record(self, message: ContentDelta) -> None:
        """Write one line for a chunk the backend is broadcasting.

        Never raises: a trace that breaks the stream it traces is worse
        than no trace at all.
        """
        if not self._enabled:
            return
        try:
            self._record(message)
        except Exception:
            logger.debug("the chunk trace could not be written", exc_info=True)

    def _record(self, message: ContentDelta) -> None:
        """Write the line itself."""
        text = message.text
        line = (
            f"[chunk tx] seq={message.event_seq} "
            f"len={len(text)} "
            f"thinking={message.is_thinking} "
            f"preview={text[:40]!r}"
        )
        if not layers.current().log_files():
            # A layer that keeps no log files (the cloud's): its log.
            logger.info(line)
            return
        self._write(f"{_timestamp()} {line}\n")

    def _write(self, line: str) -> None:
        """Append one line to the trace file."""
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()


def _timestamp() -> str:
    """The trace file's timestamp — the logging format's own."""
    now = time.time()
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)) + f",{int(now % 1 * 1000):03d}"
