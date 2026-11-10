"""Logs in the cloud: JSON lines on standard output.

In the cloud (:class:`igni_cloud.layer.CloudLayer`) nothing writes a log
file: the node collects the VM's standard output. Every logger that
writes a file on the desktop writes here instead, one JSON object per
line — ``ts``, ``level``, ``logger``, ``message``, and whatever
structured :class:`~igni_core.core.log_fields.LogFields` a record
carries (the audit log's entries, say).

Written by a thread of its own, never by the code that logs: in a VM
standard output is the serial console, where each byte is a trap to the
host, and a line written from the event loop held every request behind
it — on a resume, the first answer among them. The loop only queues the
record (:class:`StdoutLog`); :meth:`StdoutLog.flush` waits for the
thread to have written what was queued.

After a restore the writer is not even woken until the first answer
and the burst of requests behind it are through
(:meth:`StdoutLog.hold`, :meth:`StdoutLog.release`): formatting a line
takes the interpreter's lock from the event loop in its turn, and walks
the logging, JSON and datetime code — pages a restored VM brings back
one fault at a time, on the first answer's path. The records keep their
times and order; they are written a moment later.
"""

from __future__ import annotations

import atexit
import copy
import json
import logging
import logging.handlers
import queue
import sys
import threading
from datetime import UTC, datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from igni_core.core.log_fields import LogFields


class LogLine(BaseModel):
    """One record as its line: who logged what, when, and the fields it carries."""

    model_config = ConfigDict(frozen=True)

    #: ISO 8601, ``+00:00``: the record's own time, not the writer's.
    ts: str
    level: str
    logger: str
    message: str
    fields: LogFields | None = None
    exception: str | None = None

    def to_json(self) -> str:
        """The line: ``ts``, ``level``, ``logger``, ``message``, then each
        field section as a key of its own, then ``exception`` if there was one."""
        line = self.model_dump(mode="json", include={"ts", "level", "logger", "message"})
        if self.fields is not None:
            line.update(self.fields.sections)
        if self.exception is not None:
            line["exception"] = self.exception
        return json.dumps(line, default=str)


class JsonLineFormatter(logging.Formatter):
    """One JSON object per record."""

    def format(self, record: logging.LogRecord) -> str:
        if record.exc_info:
            exception: str | None = self.formatException(record.exc_info)
        else:
            exception = record.exc_text or None
        return LogLine(
            ts=datetime.fromtimestamp(record.created, UTC).isoformat(),
            level=record.levelname,
            logger=record.name,
            message=record.getMessage(),
            fields=LogFields.carried_by(record),
            exception=exception,
        ).to_json()


class QueuedRecords(logging.handlers.QueueHandler):
    """Queues a record whole — its fields, and its exception as text — for the writer."""

    #: Held at most this many records; past it they are queued as they come.
    MAX_HELD: ClassVar[int] = 1000

    def __init__(self, records: queue.Queue[logging.LogRecord]) -> None:
        super().__init__(records)
        self._held: list[logging.LogRecord] | None = None

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        prepared = copy.copy(record)
        prepared.msg = record.getMessage()
        prepared.args = None
        if record.exc_info:
            prepared.exc_text = logging.Formatter().formatException(record.exc_info)
            prepared.exc_info = None
        return prepared

    def enqueue(self, record: logging.LogRecord) -> None:
        held = self._held
        if held is not None and len(held) < self.MAX_HELD:
            held.append(record)
            return
        super().enqueue(record)

    def hold_writer(self) -> None:
        """Keep what is logged from the writer until :meth:`wake_writer`.

        (Not ``hold``/``release``: :class:`logging.Handler` has a ``release`` of its own, its lock's.)
        """
        with self.lock:  # type: ignore[union-attr]
            if self._held is None:
                self._held = []

    def wake_writer(self) -> None:
        """Hand what was held to the writer, in order."""
        with self.lock:  # type: ignore[union-attr]
            held, self._held = self._held, None
            for record in held or ():
                super().enqueue(record)


class StdoutLog:
    """A standard-output handler, and the loggers it serves.

    The cloud layer holds the process's one (:class:`igni_cloud.layer.CloudLayer`)
    and hands it to the guest contract, which holds the writer after a restore.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handler: QueuedRecords | None = None
        self._queue: queue.Queue[logging.LogRecord] | None = None
        self._writer: logging.handlers.QueueListener | None = None

    def handler(self) -> logging.Handler:
        """What loggers get: a handler that queues; a thread writes the line."""
        with self._lock:
            if self._handler is None:
                stream = logging.StreamHandler(sys.stdout)
                stream.setFormatter(JsonLineFormatter())
                records: queue.Queue[logging.LogRecord] = queue.Queue()
                writer = logging.handlers.QueueListener(records, stream)
                writer.start()
                self._queue, self._writer = records, writer
                self._handler = QueuedRecords(records)
                # What was logged last is written before the process ends.
                atexit.register(self.reset)
            return self._handler

    def hold(self) -> None:
        """Queue nothing for the writer until :meth:`release` (after a restore's first answer)."""
        if self._handler is not None:
            self._handler.hold_writer()

    def release(self) -> None:
        """Let the writer have what was held."""
        if self._handler is not None:
            self._handler.wake_writer()

    def flush(self) -> None:
        """Until every record queued so far is written."""
        records = self._queue
        if records is not None:
            records.join()

    def reset(self) -> None:
        """Write what is queued and forget the handler, so the next one
        writes to the current ``sys.stdout`` (tests capture it)."""
        with self._lock:
            if self._handler is not None:
                self._handler.wake_writer()
            writer = self._writer
            self._handler = self._queue = self._writer = None
        if writer is not None:
            atexit.unregister(self.reset)
            writer.stop()

    def attach(self, name: str, level: int = logging.INFO) -> logging.Logger:
        """Send ``name``'s records to standard output (once)."""
        target = logging.getLogger(name)
        handler = self.handler()
        if handler not in target.handlers:
            target.addHandler(handler)
        if target.level == logging.NOTSET or target.level > level:
            target.setLevel(level)
        return target


__all__ = ["JsonLineFormatter", "LogLine", "QueuedRecords", "StdoutLog"]
