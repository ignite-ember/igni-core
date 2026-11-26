"""Audit logging — records tool executions to a JSONL file.

Architecture — everything is on :class:`AuditLogger`:

* :class:`AuditLogger` — thin IO orchestrator. Constructor
  captures the log path and ensures its parent directory
  exists; :meth:`log` writes one typed :class:`AuditEntry` to
  disk and returns an :class:`AuditWriteResult` envelope.
* :class:`AuditEntry` (in the sibling schemas module) owns the
  wire shape and the factory classmethods (``success`` /
  ``error`` / ``blocked`` / ``tool_call``). :class:`AuditLogger`
  never inspects the entry's contents — the write path is a
  single :meth:`AuditEntry.to_jsonl_line` call.

Every Pydantic model in this module comes from
:mod:`.audit_schemas` and is re-exported here so external callers
keep a single import site (matches the ``update_checker.py`` /
``display.py`` re-export convention).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from igni_core.core.config.settings import Settings
from igni_core.core.layer import layers
from igni_core.core.log_fields import LogFields
from igni_core.core.utils.audit_schemas import (
    AuditDetails,
    AuditEntry,
    AuditStatus,
    AuditWriteResult,
    BlockedDetails,
    ErrorDetails,
    SuccessDetails,
    TerminalDetails,
    ToolCallDetails,
)

# Re-exports so callers keep the single-import-site convention.
__all__ = [
    "AuditDetails",
    "AuditEntry",
    "AuditLogger",
    "AuditStatus",
    "AuditWriteResult",
    "BlockedDetails",
    "ErrorDetails",
    "SuccessDetails",
    "TerminalDetails",
    "ToolCallDetails",
]


logger = logging.getLogger(__name__)


class AuditLogger:
    """Logs tool executions to a JSON-lines file.

    Single-concern IO orchestrator:

    * Constructor captures the target path and creates the
      parent directory.
    * :meth:`log` accepts a typed :class:`AuditEntry` (built via
      its ``.success`` / ``.error`` / ``.blocked`` / ``.tool_call``
      factories at the call site), appends one JSONL line, and
      returns an :class:`AuditWriteResult` envelope.

    IO failures (full disk, read-only mount, permission denied)
    surface through the returned :class:`AuditWriteResult` and are
    counted (:attr:`failed_writes`). A warning carrying the count is
    emitted on the first failure and again at every power of ten
    (10th, 100th, ...), so a read-only-disk session neither hides the
    loss nor writes one warning per tool call.
    """

    #: Where entries go when the layer keeps no log files (``core/layer.py``).
    CLOUD_LOGGER = logging.getLogger("igni_core.audit")

    def __init__(self, settings: Settings) -> None:
        self._log_path = Path(settings.storage.audit_log).expanduser()
        #: A layer that keeps no log files keeps no audit file (``core/layer.py``).
        self._to_stdout = not layers.current().log_files()
        if not self._to_stdout:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
        # Every failed write is counted; the count is warned about on
        # the first and at each power of ten.
        self._failed_writes = 0

    @property
    def log_path(self) -> Path:
        """Absolute path to the audit-log file this instance
        writes to.
        """
        return self._log_path

    @property
    def failed_writes(self) -> int:
        """How many entries this instance could not write."""
        return self._failed_writes

    def log(self, entry: AuditEntry) -> AuditWriteResult:
        """Append one :class:`AuditEntry` to the log.

        Returns an :class:`AuditWriteResult` — ``ok=True`` on
        success, ``ok=False`` with a human-readable ``reason`` on
        IO failure. Never raises: audit is fire-and-forget by
        design; a logging failure must not break an in-flight
        tool call.
        """
        if self._to_stdout:
            self.CLOUD_LOGGER.info(
                "audit",
                extra=LogFields(sections={"audit": json.loads(entry.to_jsonl_line())}).extra(),
            )
            return AuditWriteResult(ok=True)
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(entry.to_jsonl_line())
        except OSError as exc:
            return self._failed(str(exc))
        return AuditWriteResult(ok=True)

    def _failed(self, reason: str) -> AuditWriteResult:
        """Count one lost entry, and warn on the 1st, 10th, 100th, ..."""
        self._failed_writes += 1
        count = self._failed_writes
        if str(count).rstrip("0") == "1":
            logger.warning(
                "Audit log write failed (%s); %d entr%s lost so far.",
                reason,
                count,
                "y" if count == 1 else "ies",
            )
        return AuditWriteResult(ok=False, reason=reason)
