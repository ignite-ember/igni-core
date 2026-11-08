"""Typed view models + wire schemas for the context / compaction /
history-truncation surface.

Extracted out of :mod:`igni_core.backend.cmd_context` and
:mod:`igni_core.backend.server_context` — the wire types the
:class:`ContextController` RPCs return live here now, alongside
the pre-existing chat-view models.

Wire schemas (RPC-returned Pydantic models):

* :class:`TruncateHistoryResult` — ``ContextController.truncate_history``
  result: ``removed`` run-count + optional ``error`` string.
* :class:`PendingMessage` — one pre-persisted user message row
  surfaced by ``ContextController.get_pending_messages``.
  ``role`` is always ``"user"`` — encapsulated via
  :meth:`PendingMessage.from_pending_row`.
* :data:`PENDING_STALENESS_SECONDS` — how stale a pending row
  must be before we surface it as "interrupted".

Chat-view models (slash-command output):

* :class:`OutputStylesListView` — the ``/output-style list``
  chat card. Wraps the raw ``styles`` mapping plus the currently
  active style name and renders the ``**Output styles**``
  markdown list (or the empty-state info card).
* :class:`OutputStyleStatusView` — the ``/output-style status``
  / ``show`` info card, with a ``(none)`` sentinel for the
  empty-string case.
* :class:`ContextBreakdownView` — the ``/ctx`` markdown card.
  Owns the 7-line template plus the ``runs/total*100``
  percentage calculation; the domain
  :class:`ContextBreakdown` in
  :mod:`igni_core.core.session.schemas` stays presentation-
  free (no ``CommandResult`` import in the domain layer).

``OutputStyle`` is a plain ``@dataclass``; views that wrap it set
``arbitrary_types_allowed=True``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from igni_core.backend.command_result import CommandResult
from igni_core.core.output_styles import OutputStyle
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.session.pending_messages import InterruptedMessage as InterruptedMessageRow
from igni_core.core.session.pending_messages import InterruptedReason
from igni_core.core.session.pending_messages import PendingMessage as PendingMessageRow

if TYPE_CHECKING:
    from igni_core.core.session.schemas import ContextBreakdown


# How stale a pending-message row must be before we surface it
# as an "interrupted" banner. A fresh pending row almost always
# means "Agno is still finishing its post-stream tail" (it can
# take 15-30 s). The banner is meant for actual crashes across
# BE restarts — 60 s makes a reload during the tail stay quiet.
PENDING_STALENESS_SECONDS = 60


class TruncateHistoryResult(BaseModel):
    """Wire shape for :meth:`ContextController.truncate_history` —
    ``removed`` is the count of runs dropped (0 on any failure);
    ``error`` is empty on success."""

    removed: int
    error: str = ""


class PendingMessage(BaseModel):
    """One pre-persisted user message row surfaced by
    :meth:`ContextController.get_pending_messages`. ``role`` is
    always ``"user"`` — only user turns get pre-persisted.

    Build one from a storage-row dataclass via
    :meth:`from_pending_row`.
    """

    role: Literal["user"] = "user"
    content: str
    received_at: int
    message_id: str

    @classmethod
    def from_pending_row(cls, row: PendingMessageRow) -> PendingMessage:
        """Project a domain :class:`PendingMessage` storage row
        (the ``@dataclass`` in
        :mod:`igni_core.core.session.pending_messages`) onto the
        wire model."""
        return cls(
            content=row.text,
            received_at=row.received_at,
            message_id=row.message_id,
        )


class InterruptedRun(BaseModel):
    """Wire shape for one explicitly-interrupted run surfaced by
    :meth:`ContextController.get_interrupted_runs`.

    A row is "interrupted" when the cancel or error path in
    :class:`RunController` stamped it with ``interrupted_at`` and
    ``interrupted_reason``. The FE renders this as the banner on
    the assistant bubble that was in-flight when the run stopped,
    with Retry / Discard / Edit-prompt actions.

    ``reason`` is the storage column's :data:`InterruptedReason`.
    ``last_error`` is set when
    ``reason == "errored"`` and ``None`` otherwise.
    """

    message_id: str
    content: str
    received_at: int
    interrupted_at: int
    reason: InterruptedReason
    last_error: str | None = None

    @classmethod
    def from_interrupted_row(
        cls,
        row: InterruptedMessageRow,
    ) -> InterruptedRun:
        """Project a domain :class:`InterruptedMessage` storage row
        onto the wire model. Keeps the controller free of literal
        field-by-field mapping — see the sibling
        :meth:`PendingMessage.from_pending_row`."""
        return cls(
            message_id=row.message_id,
            content=row.text,
            received_at=row.received_at,
            interrupted_at=row.interrupted_at,
            reason=row.interrupted_reason,
            last_error=row.last_error,
        )


class DiscardInterruptedRunResult(BaseModel):
    """Wire shape for :meth:`ContextController.discard_interrupted_run`.
    ``ok`` is ``False`` when the row didn't exist (idempotent — the
    FE can treat the row as gone regardless)."""

    ok: bool
    error: str = ""


class OutputStylesListView(BaseModel):
    """Wraps the discovered ``styles`` mapping + the active style
    name for the ``/output-style list`` chat card.

    Empty-styles branch renders the "no output styles configured"
    info card (with a one-liner nudge to drop a markdown file
    into ``.igni/output-styles/``). The populated branch renders
    a sorted bullet list, marking the active style with
    ``(active)`` and falling back to ``_(no description)_`` when
    a style leaves its ``description`` blank.

    ``OutputStyle`` is a plain ``@dataclass`` (not a Pydantic
    model), so we opt into ``arbitrary_types_allowed=True``. Wrapping the raw mapping keeps
    this view thin; the coordinator doesn't have to pre-project
    into ``(name, description, active)`` triples.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    styles: dict[str, OutputStyle] = Field(default_factory=dict)
    active: str = ""

    def to_command_result(self) -> CommandResult:
        if not self.styles:
            return CommandResult.info(
                "No output styles configured. Drop a markdown file at "
                f"`{CONFIG_DIR}/output-styles/<name>.md` (frontmatter: `name`, "
                "`description`; body is the system-prompt extension)."
            )
        lines = ["**Output styles**", ""]
        for name in sorted(self.styles):
            marker = " (active)" if name == self.active else ""
            desc = self.styles[name].description or "_(no description)_"
            lines.append(f"- `{name}`{marker} — {desc}")
        lines.append("")
        lines.append("Switch with `/output-style <name>`.")
        return CommandResult.markdown("\n".join(lines))


class OutputStyleStatusView(BaseModel):
    """Wraps the active-style name for the ``/output-style status``
    (or ``show``) info card.

    ``active`` is the raw string returned by
    :attr:`Session.active_output_style`; the empty-string sentinel
    the session emits when no style has been picked yet is
    rendered as ``(none)`` in the info card, preserving the
    original ``active or '(none)'`` behavior.
    """

    active: str | None = None

    def to_command_result(self) -> CommandResult:
        return CommandResult.info(f"Active output style: **{self.active or '(none)'}**")


class ContextBreakdownView(BaseModel):
    """The ``/ctx`` markdown card for a domain :class:`ContextBreakdown`.

    Owns the ``**Context breakdown**`` template plus the
    ``runs/total*100`` percentage calculation, so the domain model in
    :mod:`core.session.schemas` stays render-free. Built via
    :meth:`from_domain`.
    """

    total: int
    runs: int
    floor: int

    @classmethod
    def from_domain(cls, breakdown: ContextBreakdown) -> ContextBreakdownView:
        return cls(total=breakdown.total, runs=breakdown.runs, floor=breakdown.floor)

    @property
    def runs_percent(self) -> float:
        return (self.runs / self.total * 100.0) if self.total else 0.0

    def to_command_result(self) -> CommandResult:
        lines = [
            "**Context breakdown**",
            "",
            f"- **Total:** {self.total:,} tokens",
            f"- **Conversation (runs):** {self.runs:,} tokens ({self.runs_percent:.1f}% of total)",
            f"- **Floor (system + tools + rules + memories + summary):** {self.floor:,} tokens",
            "",
            "`/compact` only clears the conversation portion — the floor "
            "is rebaked into every prompt and cannot be compacted away.",
        ]
        return CommandResult.markdown("\n".join(lines))


__all__ = [
    "PENDING_STALENESS_SECONDS",
    "TruncateHistoryResult",
    "PendingMessage",
    "InterruptedRun",
    "DiscardInterruptedRunResult",
    "OutputStylesListView",
    "OutputStyleStatusView",
    "ContextBreakdownView",
]
