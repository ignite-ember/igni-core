"""Scheduler data models.

Domain types for the deferred-task queue: :class:`TaskStatus` (the
task lifecycle) and :class:`ScheduledTask` (one queued task).

These types are view-free. The chat renderings live in
:mod:`igni_core.backend.schemas_scheduler`, and a frontend that wants
a status glyph maps from the enum on its own side — the same split
the sibling ``schemas_loop.py`` views make.
"""

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator

from igni_core.core.scheduler.recurrence import Recurrence


class TaskStatus(str, Enum):
    """Lifecycle of one scheduled task.

    ``pending`` (waiting for its time), ``running`` (executing now),
    ``completed`` (finished successfully), ``failed`` (finished with an
    error) or ``cancelled`` (cancelled before it ran).
    """

    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"

    @property
    def is_active(self) -> bool:
        """Domain fact: ``pending`` and ``running`` are the not-done states.

        Kept as a pure enum concern (no view coupling) so dispatch
        sites ask ``task.status.is_active`` rather than repeating the
        pair of literals.
        """
        return self in (TaskStatus.pending, TaskStatus.running)


# ``recurrence`` accepts only the canonical ``every N <unit>`` form (the
# field validator below enforces it). User-facing phrases ("daily",
# "weekly") must go through Recurrence.parse and be stored as
# ``.canonical()``. After completion, the runner creates the next
# occurrence automatically.
class ScheduledTask(BaseModel):
    """A prompt scheduled to run at a later time, once or repeatedly.

    When a recurring task completes, igni schedules its next occurrence
    as a new task.
    """

    id: str = Field(description="Short identifier of the task (8 hex characters).")
    description: str = Field(description="What the task does: the prompt igni runs when it is due.")
    scheduled_at: datetime = Field(
        description="When the task is due to run, in the machine's local time."
    )
    created_at: datetime = Field(
        default_factory=datetime.now,
        description="When the task was scheduled, in the machine's local time.",
    )
    status: TaskStatus = Field(
        default=TaskStatus.pending, description="Where the task is in its lifecycle."
    )
    result: str = Field(
        default="", description="Output of the run once it completed. Empty until then."
    )
    error: str = Field(
        default="", description="Error message when the run failed. Empty otherwise."
    )
    recurrence: str = Field(
        default="",
        description="Repeat pattern in the form `every N <minutes|hours|days|weeks>` (e.g. "
        "`every 1 days`, `every 7 days`). Empty for a one-time task.",
    )

    @classmethod
    def new(
        cls,
        description: str,
        scheduled_at: datetime,
        recurrence: str = "",
    ) -> "ScheduledTask":
        """Construct a fresh task with a generated 8-hex-char id.

        Owns the id-generation invariant so callers stop duplicating
        ``uuid.uuid4().hex[:8]`` across the four call sites that
        schedule a task — the backend ``/schedule`` coordinator, the
        two paths of the agent-facing
        :class:`~igni_core.core.tools.schedule.ScheduleTools` toolkit,
        and the recurring-task rescheduler in
        :class:`~igni_core.core.scheduler.runner.SchedulerRunner`.
        Same "behaviour on the model" pattern as the
        :meth:`TaskStatus.is_active` property above.
        """
        return cls(
            id=uuid.uuid4().hex[:8],
            description=description,
            scheduled_at=scheduled_at,
            recurrence=recurrence,
        )

    @field_validator("recurrence")
    @classmethod
    def _recurrence_must_be_canonical(cls, v: str) -> str:
        """Fail loudly if the recurrence string isn't the canonical form.

        The DB column stores a plain string for wire-compatibility, but
        every non-empty value must round-trip through
        :meth:`Recurrence.from_canonical` — otherwise ``recurrence_obj``
        would silently return ``None`` and the rescheduler would drop
        the recurring task without an error at construction time.
        """
        if v and Recurrence.from_canonical(v) is None:
            raise ValueError(f"recurrence must be canonical (e.g. 'every 1 days'), got {v!r}")
        return v

    @property
    def recurrence_obj(self) -> Recurrence | None:
        """Typed view of the persisted recurrence string.

        Returns ``None`` for one-shot tasks (``recurrence == ""``) so
        callers can guard with a single ``if task.recurrence_obj``
        check instead of re-parsing the canonical string at every use
        site. Replaces the old ``next_occurrence_from_recurrence``
        free-function — the operation now lives on the value object.
        """
        return Recurrence.from_canonical(self.recurrence)
