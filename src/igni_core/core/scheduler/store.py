"""Per-project store for scheduled tasks (async via SQLAlchemy).

On the desktop tasks live in ``~/.igni/projects/<project_hash>/state.db``;
in the cloud in the shared database, filtered by the project's scope.
Either way each project has its own scheduler queue — switching projects
doesn't surface another project's pending ``/loop`` jobs.

The store owns queries and nothing else: a row's own mapping to and
from the domain task lives on
:class:`~igni_core.core.scheduler.db_models.ScheduledTaskModel`, which
knows its columns and the format they hold.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from sqlalchemy import Select, delete, func, select

from igni_core.core.activity import ActivityRegistry
from igni_core.core.db.database import Database
from igni_core.core.db.locator import StorageLocator
from igni_core.core.scheduler.db_models import ScheduledTaskModel
from igni_core.core.scheduler.models import ScheduledTask, TaskStatus


class TaskStore:
    """Persists scheduled tasks in the project's database (async)."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        project_dir: str | Path | None = None,
        db: Database | None = None,
    ) -> None:
        if db is not None:
            self._db = db
            return
        self._db = StorageLocator.database_for(db_path, project_dir)

    async def add(self, task: ScheduledTask) -> None:
        async with self._db.session() as session, session.begin():
            row = ScheduledTaskModel.from_task(task)
            for column, value in self._db.scope.stamp().items():
                setattr(row, column, value)
            session.add(row)
        # A runner sleeping until the old earliest task looks again.
        ActivityRegistry.shared().rearm()

    async def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        result: str = "",
        error: str = "",
        *,
        unless_cancelled: bool = False,
    ) -> bool:
        """Write a task's status. Returns ``True`` if the write landed.

        ``unless_cancelled`` makes the write lose to a cancel that
        arrived first. The runner needs it: its execution window is up
        to ``task_timeout`` (300s by default) and a user only reaches
        for cancel *because* the task is misbehaving, so an
        unconditional terminal write at the end of that window silently
        overwrote the ``cancelled`` the user had just set — and the
        recurring task the cancel promised to stop was then rescheduled.
        The read and the write share one transaction, so the cancel
        either commits before it (and wins) or after it (and stands).
        """
        async with self._db.session() as session, session.begin():
            row = (
                await session.execute(self._select_one(task_id).with_for_update())
            ).scalar_one_or_none()
            if row is None:
                return False
            if unless_cancelled and row.status == TaskStatus.cancelled.value:
                return False
            row.status = status.value
            row.result = result
            row.error = error
            return True

    async def cancel(self, task_id: str) -> TaskStatus | None:
        """Mark a task cancelled, if it has not finished; its status before.

        ``None`` for a task the store does not have (or cannot read). A task that already
        completed, failed or was cancelled keeps its status, its result and
        its error: cancelling it is not a write. The read and the write
        share one transaction, as in :meth:`update_status`.
        """
        async with self._db.session() as session, session.begin():
            row = (
                await session.execute(self._select_one(task_id).with_for_update())
            ).scalar_one_or_none()
            if row is None:
                return None
            try:
                before = TaskStatus(row.status)
            except ValueError:
                # A row this build cannot read is, to every other call, no
                # task at all: cancelling it is not found, not a crash.
                return None
            if before.is_active:
                row.status = TaskStatus.cancelled.value
            return before

    async def remove(self, task_id: str) -> bool:
        async with self._db.session() as session, session.begin():
            result = await session.execute(
                delete(ScheduledTaskModel).where(
                    self._db.scope.where(ScheduledTaskModel), ScheduledTaskModel.id == task_id
                )
            )
            return (result.rowcount or 0) > 0

    async def get_due_tasks(self) -> list[ScheduledTask]:
        """Get all pending tasks whose scheduled time has passed."""
        now = datetime.now()
        async with self._db.session() as session:
            result = await session.execute(
                select(ScheduledTaskModel)
                .where(
                    self._db.scope.where(ScheduledTaskModel),
                    ScheduledTaskModel.status == TaskStatus.pending.value,
                    ScheduledTaskModel.scheduled_at <= now,
                )
                .order_by(ScheduledTaskModel.scheduled_at)
            )
            rows = result.scalars().all()
        return self._tasks(rows)

    async def next_pending_at(self) -> datetime | None:
        """When the earliest pending task is due, or ``None``."""
        async with self._db.session() as session:
            result = await session.execute(
                select(func.min(ScheduledTaskModel.scheduled_at)).where(
                    self._db.scope.where(ScheduledTaskModel),
                    ScheduledTaskModel.status == TaskStatus.pending.value,
                )
            )
            return result.scalar_one_or_none()

    async def get_all(self, include_done: bool = False) -> list[ScheduledTask]:
        """Get all tasks, optionally including completed/failed/cancelled.

        Ordered by ``scheduled_at`` descending — tasks set to run
        furthest in the future surface first. Both the TUI tasks
        panel and the agent-facing ``list_scheduled_tasks`` tool
        render the store's order verbatim.
        """
        stmt = (
            select(ScheduledTaskModel)
            .where(self._db.scope.where(ScheduledTaskModel))
            .order_by(ScheduledTaskModel.scheduled_at.desc())
        )
        if not include_done:
            stmt = stmt.where(
                ScheduledTaskModel.status.in_((TaskStatus.pending.value, TaskStatus.running.value))
            )
        async with self._db.session() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()
        return self._tasks(rows)

    async def get(self, task_id: str) -> ScheduledTask | None:
        """The task with this id, or ``None`` when there is no such row —
        or when the row is one this build cannot read."""
        async with self._db.session() as session:
            row = (await session.execute(self._select_one(task_id))).scalar_one_or_none()
        return row.to_task() if row is not None else None

    def _select_one(self, task_id: str) -> Select[tuple[ScheduledTaskModel]]:
        return select(ScheduledTaskModel).where(
            self._db.scope.where(ScheduledTaskModel), ScheduledTaskModel.id == task_id
        )

    def _tasks(self, rows: Sequence[ScheduledTaskModel]) -> list[ScheduledTask]:
        """The tasks these rows hold, minus the ones that can't be read.

        A row the domain refuses costs only itself: it is left out, so
        one unreadable row cannot stop the runner's poll loop.
        """
        tasks: list[ScheduledTask] = []
        for row in rows:
            task = row.to_task()
            if task is not None:
                tasks.append(task)
        return tasks
