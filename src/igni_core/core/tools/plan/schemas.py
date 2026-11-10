"""Typed payload models for the plan-mode toolkit.

Every broadcast the plan tool emits, plus the ``tasks`` argument
of ``exit_plan_mode``, is typed via a Pydantic model here. The
broadcasts are :class:`BroadcastEvent` subclasses; the transport
serialises them once, at the push boundary.

The task rows are typed end to end: the agent's loose JSON rows
become :class:`PlanTaskInput` models, and the checklist row they
turn into — the one the ``plan_submitted`` broadcast and the
``PlanExitResult`` envelope carry — is a
:class:`~igni_core.core.tools.todo.TodoItemWire`, the same wire
shape the todos panel already speaks.

Beyond the broadcast payloads, this module also hosts the typed
envelopes the plan transactions hand back —
:class:`PlanEnterResult` for ``enter_plan_mode``,
:class:`PlanExitResult` for ``exit_plan_mode``, and
:class:`PlanSubmitDecision` for what the ``PlanSubmit`` gate
decided. :class:`PlanTool` consumes those envelopes and renders the
agent-visible reply string from a single call site each, so the
"Error: ..." / "Plan submitted" / "Entered plan mode"
substrings the tool used to build inline now live on the
coordinator side of the boundary as ``reply_text``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    computed_field,
    model_validator,
)

from igni_core.core.broadcast_schema import BroadcastEvent, RunScopedBroadcastEvent
from igni_core.core.tools.todo import TodoItemWire


# The base mode flip is already broadcast by RuntimeModeCoordinator;
# this follow-up carries who did it and why.
class PlanModeEnteredEvent(BroadcastEvent):
    """Plan mode was entered, with who entered it and why.

    Sent on the same channel as a plain permission-mode switch, after
    it; a client can show it as a short banner explaining the switch."""

    channel: Literal["permission_mode_changed"] = "permission_mode_changed"
    kind: Literal["plan_mode_entered"] = Field(
        default="plan_mode_entered",
        description="Always `plan_mode_entered`: tells this message apart from a plain mode "
        "switch (kind `switched`) on the same channel.",
    )
    mode: Literal["default", "plan", "acceptEdits", "bypassPermissions"] = Field(
        description="Permission mode now in effect: `default` (ask before risky actions), "
        "`plan` (read-only planning, no edits), `acceptEdits` (file edits approved "
        "automatically) or `bypassPermissions` (no approval prompts)."
    )
    source: Literal["agent", "user"] = Field(
        description="Who entered plan mode: `agent` (the agent chose to plan first) or `user`."
    )
    reason: str = Field(
        default="", description="Why plan mode was entered, as given. Empty when none was given."
    )


# Queued by PlanTool.exit_plan_mode.
class PlanSubmittedEvent(RunScopedBroadcastEvent):
    """The agent submitted a plan for the user to approve or dismiss.

    ``tasks`` is the plan's initial checklist, in the same row shape
    as the ``todos_updated`` push; later ``todos_updated`` pushes
    update the rows' statuses as work proceeds."""

    channel: Literal["plan_submitted"] = "plan_submitted"
    # Redeclared from RunScopedBroadcastEvent only to describe it; same type,
    # default and serialisation.
    run_id: str | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="Run in which the plan was submitted; pass it back when approving or "
        "dismissing the plan. Omitted when not known.",
    )
    plan: str = Field(description="The plan text, in Markdown.")
    tasks: list[TodoItemWire] = Field(
        default_factory=list,
        description="Checklist of the plan's steps, all starting as `pending`. Empty when "
        "the plan has no task list.",
    )


class PlanSubmitHookPayload(BaseModel):
    """What igni's configured ``PlanSubmit`` hooks are sent."""

    session_id: str
    #: The plan, in markdown, as the extensions left it.
    plan: str
    #: Plans rejected since the agent entered plan mode: 0 for the first.
    attempt: int


class PlanSubmitDecision(BaseModel):
    """What the ``PlanSubmit`` gate decided about a plan.

    * ``allowed`` — may the plan go to the person?
    * ``plan`` — the plan as the gate left it. An extension may have
      changed it; igni's own ``PlanSubmit`` hooks then run on that.
    * ``reason`` — why not, when it does not. The agent is told the
      reason so it can do the work the plan needs and submit again.
    """

    model_config = ConfigDict(frozen=True)

    allowed: bool
    plan: str = ""
    reason: str = ""


class PlanTaskInput(BaseModel):
    """One row in ``exit_plan_mode(tasks=[...])`` — the agent-
    facing shape of a plan-time task suggestion.

    The agent sends loose JSON (``{"content": "Step 1",
    "activeForm": "Doing step 1"}``); this model is the validated
    form of one such row. Extras are ignored (not rejected) so a
    slightly wider agent-produced dict doesn't blow up the call.
    A row that cannot be normalised is dropped by
    :meth:`validate_batch`, which names the row that failed.

    The row carries no status: a submitted plan enumerates work,
    it does not schedule it. Every row it becomes starts
    ``pending`` — the status flips come later from ``todo_write``.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    content: str = ""
    active_form: str = Field(default="", alias="activeForm")

    @model_validator(mode="before")
    @classmethod
    def _normalise(cls, raw: Any) -> Any:
        """Coerce the agent's loose values into the strings this
        model holds.

        Mirrors the leniency of the todo pipeline: a numeric
        ``content`` becomes its string form rather than failing
        the whole row, and both values are stripped the way todo
        rows are. ``None`` becomes empty — there is no task in a
        ``None``."""
        if not isinstance(raw, dict):
            return raw
        normalised = dict(raw)
        for key in ("content", "activeForm", "active_form"):
            if key in normalised:
                value = normalised[key]
                normalised[key] = "" if value is None else str(value).strip()
        return normalised

    def to_wire(self) -> TodoItemWire:
        """The checklist row this plan task becomes."""
        return TodoItemWire(
            content=self.content,
            status="pending",
            active_form=self.active_form,
        )

    @classmethod
    def validate_batch(cls, rows: Iterable[Any] | None) -> PlanTaskBatch:
        """Validate a batch of agent-supplied task rows.

        Returns the rows that can be applied as typed models plus
        one error message per row that could not be used. The
        caller applies the good rows and surfaces the errors so
        the agent can correct on the next call.

        The row index in an error message uses the ``todos[i]``
        prefix the todo pipeline uses, so an agent that hits a
        bad row in ``exit_plan_mode`` and then in ``todo_write``
        sees the same row naming in both replies."""
        tasks: list[PlanTaskInput] = []
        errors: list[str] = []
        for index, entry in enumerate(rows or []):
            if not isinstance(entry, dict):
                errors.append(f"todos[{index}]: not a dict")
                continue
            try:
                task = cls.model_validate(entry)
            except ValidationError:
                errors.append(f"todos[{index}]: invalid task row")
                continue
            if not task.content:
                errors.append(f"todos[{index}]: empty content")
                continue
            tasks.append(task)
        return PlanTaskBatch(tasks=tasks, errors=errors)


class PlanTaskBatch(BaseModel):
    """The outcome of validating a batch of agent-supplied plan
    task rows: the rows that can be applied, and the rows that
    could not — one error message per rejected row."""

    model_config = ConfigDict(frozen=True)

    tasks: list[PlanTaskInput] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class PlanEnterResult(BaseModel):
    """Typed envelope :meth:`PlanTransactionCoordinator.enter`
    returns. Replaces the old "Error: ..." string channel with a
    structured ``ok`` + ``reason`` + ``reply_text`` shape so
    :class:`PlanTool` can derive its agent-visible string at a
    single render site.

    Fields:

    * ``ok`` — did the enter transaction complete? False when the
      session shape refuses the flip (no ``set_permission_mode``)
      or the evaluator reports an error string.
    * ``mode_flipped`` — did the permission mode actually change?
      A no-op "already in plan" is ``ok=True, mode_flipped=False``.
    * ``mode_status`` — the raw string
      :meth:`RuntimeModeCoordinator.set_permission_mode` returned (empty on
      refusal). Used to preserve the exact message on error paths.
    * ``reason`` — machine-readable refusal reason (empty on ok).
    * ``researcher_report`` — the plan_researcher sub-agent's
      report text when ``task=`` was provided and the spawn
      succeeded, else empty string.
    * ``error`` — human-readable error string on refusal, or
      empty on ok. Distinct from ``reason`` so callers can log
      the machine tag AND surface the prose.
    * ``reply_text`` — the string the tool returns to the agent
      verbatim. Contains the "Entered plan mode" / "Error: ..."
      substrings the tests match on.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    mode_flipped: bool = False
    mode_status: str = ""
    reason: str = ""
    researcher_report: str = ""
    error: str = ""
    reply_text: str


class PlanExitResult(BaseModel):
    """Typed envelope :meth:`PlanTransactionCoordinator.submit`
    returns. Replaces the old string-only reply with a structured
    shape carrying the store snapshot and validation errors.

    Fields:

    * ``ok`` — did the submit transaction commit? False on empty
      plan or a missing plan_store on the session.
    * ``task_snapshot`` — the TodoStore snapshot after commit, as
      :class:`TodoItemWire` rows (each carries ``content`` /
      ``status`` / ``activeForm``).
    * ``validation_errors`` — malformed task rows that
      validation dropped; surfaced in the reply so the agent can
      correct on the next call.
    * ``task_count`` — length of ``task_snapshot``; derived from
      the snapshot, so the two can never disagree.
    * ``reason`` — machine-readable refusal reason (empty on ok).
    * ``error`` — human-readable error string on hard refusals.
    * ``reply_text`` — the string the tool returns to the agent
      verbatim. Contains "Plan submitted" /
      "Error" substrings the tests match on.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    task_snapshot: list[TodoItemWire] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    reason: str = ""
    error: str = ""
    reply_text: str

    @computed_field  # type: ignore[prop-decorator]
    @property
    def task_count(self) -> int:
        """How many rows the snapshot holds."""
        return len(self.task_snapshot)


__all__ = [
    "PlanEnterResult",
    "PlanExitResult",
    "PlanModeEnteredEvent",
    "PlanSubmitDecision",
    "PlanSubmitHookPayload",
    "PlanSubmittedEvent",
    "PlanTaskBatch",
    "PlanTaskInput",
]
