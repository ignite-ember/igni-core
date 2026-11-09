"""SQLAlchemy ORM model for the scheduler tasks table.

Kept separate from ``models.py`` (Pydantic domain types) so importing
the domain models doesn't drag in SQLAlchemy. Alembic env imports this
module to register the table on ``Base.metadata``.

The row also owns the mapping in both directions —
:meth:`ScheduledTaskModel.from_task` and :meth:`ScheduledTaskModel.to_task`
— because the row knows its own columns and the format they hold. The
store that queries the table stays free of the conversion.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from igni_core.core.db.base import Base
from igni_core.core.db.scope import ScopedColumns
from igni_core.core.scheduler.models import ScheduledTask, TaskStatus
from igni_core.core.scheduler.recurrence import Recurrence

logger = logging.getLogger(__name__)


class ScheduledTaskModel(ScopedColumns, Base):
    __tablename__ = "scheduler_tasks"
    __table_args__ = (Index("ix_scheduler_tasks_scope", "org", "producer", "project"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="pending")
    result: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    error: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    recurrence: Mapped[str] = mapped_column(Text, nullable=False, server_default="")

    # ── Mapping ───────────────────────────────────────────────────

    @classmethod
    def from_task(cls, task: ScheduledTask) -> ScheduledTaskModel:
        """The row that holds ``task``.

        The scope columns are left to the writer — it stamps the row
        with the scope it writes in.
        """
        return cls(
            id=task.id,
            description=task.description,
            scheduled_at=task.scheduled_at,
            created_at=task.created_at,
            status=task.status.value,
            result=task.result,
            error=task.error,
            recurrence=task.recurrence,
        )

    def to_task(self) -> ScheduledTask | None:
        """The task this row holds, or ``None`` when the row can't be read.

        A row the domain refuses outright (an unknown status, say) has
        no task to give: it is logged and read as ``None``, so the
        caller's poll loop keeps running.
        """
        recurrence = self._readable_recurrence()
        try:
            return ScheduledTask(
                id=self.id,
                description=self.description,
                scheduled_at=self.scheduled_at,
                created_at=self.created_at,
                status=TaskStatus(self.status),
                result=self.result or "",
                error=self.error or "",
                recurrence=recurrence,
            )
        except ValueError as exc:
            logger.warning("Skipping unreadable scheduler row %s: %s", self.id, exc)
            return None

    def _readable_recurrence(self) -> str:
        """The recurrence this row holds, or ``""`` when it holds none.

        The ``recurrence`` column holds the canonical form by contract:
        the store only ever writes what :class:`ScheduledTask` accepts.
        A row holding anything else is data this build does not
        recognise — written by an older version of igni, or corrupted —
        so the task is read as one-shot rather than guessed at. One
        such row costs its own repeat and nothing else.

        The parse is guarded, because
        :meth:`~igni_core.core.scheduler.recurrence.Recurrence.from_canonical`
        turns the amount into an ``int`` and an absurd one (a number with
        thousands of digits) raises ``ValueError``. An exception here
        would escape :meth:`to_task` into the runner's poll loop, where
        the row stays pending and due: every later poll would abort on
        it the same way, and no task in the project would ever run
        again.
        """
        raw = self.recurrence or ""
        if not raw:
            return ""
        try:
            recognised = Recurrence.from_canonical(raw) is not None
        except ValueError as exc:
            logger.warning(
                "Task %s holds an unreadable recurrence (%s) — reading it as one-shot",
                self.id,
                exc,
            )
            return ""
        if not recognised:
            logger.warning(
                "Task %s holds an unrecognised recurrence %r — reading it as one-shot",
                self.id,
                raw,
            )
            return ""
        return raw
