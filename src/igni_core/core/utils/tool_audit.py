"""One audit-log entry per tool call, whoever made it and however it ended.

docs/SECURITY.md §6 promises that every tool execution is logged. Every
tool call an agent makes passes one of two Agno tool hooks — the main
agent's :class:`~igni_core.core.hooks.tool_hook.ToolEventHook`, or the
:class:`~igni_core.core.hooks.tool_audit_hook.ToolAuditHook` every pool
agent (so every sub-agent, at any depth) carries — and a call that was
paused for approval and then refused never reaches either, so the places
that refuse a paused call write its entry instead. Each of them reports
here, once per call.

What is written is what an exported OpenTelemetry span keeps when content
is not captured (``core/observability/content.py``): the tool, the agent,
the decision and what made it, the outcome and the duration. Argument
values and error messages are never written — only the arguments' names
and the exception's type — because a command, a path or a failure's text
can carry a secret.

Writing never gets in the way of the call: :meth:`ToolAudit.ran` and
:meth:`ToolAudit.refused` never raise. A failed write is counted
(:attr:`ToolAudit.failed`) and warned about by the
:class:`~igni_core.core.utils.audit.AuditLogger`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any

from igni_core.core.utils.audit import AuditEntry, AuditLogger, ToolCallDetails

logger = logging.getLogger(__name__)


class DecidedBy(StrEnum):
    """What decided whether a tool call ran."""

    #: An ``allow``, ``ask`` or ``deny`` rule in ``permissions``.
    RULE = "rule"
    #: The permission mode (plan, acceptEdits, bypassPermissions, dontAsk,
    #: ``--read-only``).
    MODE = "mode"
    #: Nothing more specific: the tool's configured permission level, with
    #: no rule or mode deciding.
    POLICY = "policy"
    #: A configured ``PreToolUse`` hook.
    HOOK = "hook"
    #: An extension's ``PreToolUse`` hook.
    EXTENSION = "extension"
    #: The person, answering an approval prompt.
    PERSON = "person"
    #: ``safety.protected_paths``.
    PROTECTED_PATH = "protected_path"
    #: ``safety.blocked_commands``.
    BLOCKED_COMMAND = "blocked_command"
    #: Bypass mode's last check (``safety.bypass_guard``).
    BYPASS_GUARD = "bypass_guard"

    @classmethod
    def of_policy(cls, source: str) -> DecidedBy:
        """What decided, by the permission evaluator step that produced
        its outcome (``PermissionOutcome.source``): a rule list, the mode,
        or neither."""
        if source in ("deny", "ask", "allow"):
            return cls.RULE
        if source == "mode":
            return cls.MODE
        return cls.POLICY


class ToolAudit:
    """Writes the session's tool-call entries to its audit log.

    ``session_id_ref`` is read per entry: the session's id is the one
    live when the call is made.
    """

    def __init__(self, audit: AuditLogger, session_id_ref: Callable[[], str]) -> None:
        self._audit = audit
        self._session_id_ref = session_id_ref
        self._failed = 0

    @property
    def failed(self) -> int:
        """Entries that could not be written: a failed write, or a failure building one."""
        return self._failed

    def ran(
        self,
        *,
        agent: str,
        tool: str,
        args: Mapping[str, Any] | None,
        decided_by: DecidedBy,
        mode: str | None = None,
        reason: str | None = None,
        duration_ms: float,
        error_type: str | None = None,
    ) -> None:
        """A call that was allowed and ran — returned, or failed with ``error_type``."""
        self._write(
            agent,
            tool,
            lambda: ToolCallDetails(
                decision="allow",
                decided_by=decided_by.value,
                mode=mode,
                reason=reason or None,
                arg_names=self.arg_names(args),
                duration_ms=round(duration_ms, 3),
                error_type=error_type,
            ),
        )

    def refused(
        self,
        *,
        agent: str,
        tool: str,
        args: Mapping[str, Any] | None,
        decided_by: DecidedBy,
        mode: str | None = None,
        reason: str | None = None,
    ) -> None:
        """A call that was refused before it ran."""
        self._write(
            agent,
            tool,
            lambda: ToolCallDetails(
                decision="deny",
                decided_by=decided_by.value,
                mode=mode,
                reason=reason or None,
                arg_names=self.arg_names(args),
            ),
        )

    @staticmethod
    def arg_names(args: Mapping[str, Any] | None) -> list[str]:
        """The arguments' names, sorted; their values stay out of the log."""
        if not isinstance(args, Mapping):
            return []
        return sorted(str(name) for name in args)

    def _write(self, agent: str, tool: str, details: Callable[[], ToolCallDetails]) -> None:
        try:
            entry = AuditEntry.tool_call(
                session_id=self._session_id_ref(),
                agent_name=agent,
                tool_name=tool,
                details=details(),
            )
            written = self._audit.log(entry).ok
        except Exception as exc:  # noqa: BLE001 — the audit must never break a tool call
            written = False
            logger.warning("could not write the audit entry for %s: %s", tool, exc)
        if not written:
            self._failed += 1


__all__ = ["DecidedBy", "ToolAudit"]
