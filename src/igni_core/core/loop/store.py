"""Per-project stores for the active ``/loop`` and its progress.

Both stores share the project's database (the desktop's ``state.db``,
the same file the scheduler writes to) and filter every query by its
:class:`~igni_core.core.db.scope.StorageScope`, so switching projects
doesn't expose another project's loop or its progress.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import Select, delete, select

from igni_core.core.db.database import Database
from igni_core.core.db.locator import StorageLocator
from igni_core.core.db.upsert import ConflictInsert
from igni_core.core.loop.db_models import LoopProgressModel, LoopStateModel
from igni_core.core.loop.models import LoopState

# ── LoopStore ─────────────────────────────────────────────────────


class LoopStore:
    """Persists the active ``/loop`` state in the single-row
    ``loop_state`` table.

    Three operations: ``load`` reads the row (or ``None`` when no
    loop is active), ``save`` upserts the row, ``clear`` deletes
    it. Every state mutation on :class:`Session` calls ``save``
    (or ``clear``) so a restart sees the latest state.
    """

    _SINGLETON_ID = 1

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

    def _select_row(self) -> Select[tuple[LoopStateModel]]:
        return select(LoopStateModel).where(
            self._db.scope.where(LoopStateModel), LoopStateModel.id == self._SINGLETON_ID
        )

    async def load(self) -> LoopState | None:
        async with self._db.session() as session:
            row = (await session.execute(self._select_row())).scalar_one_or_none()
            if row is None:
                return None
            return LoopState(
                run_id=row.run_id,
                prompt=row.prompt,
                iteration_index=row.iteration_index,
                iterations_remaining=row.iterations_remaining,
                cap_explicit=row.cap_explicit,
            )

    async def save(self, state: LoopState) -> None:
        """Upsert the singleton row.

        Created on first ``save``; subsequent calls update in
        place — ``created_at`` is preserved from the first save,
        ``updated_at`` ticks every time.
        """
        now = datetime.now()
        fields = {
            "run_id": state.run_id,
            "prompt": state.prompt,
            "iteration_index": state.iteration_index,
            "iterations_remaining": state.iterations_remaining,
            "cap_explicit": state.cap_explicit,
        }
        async with self._db.session() as session, session.begin():
            stmt = ConflictInsert.into(session, LoopStateModel).values(
                **self._db.scope.stamp(),
                id=self._SINGLETON_ID,
                **fields,
                created_at=now,
                updated_at=now,
            )
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["org", "producer", "project", "id"],
                    set_={**fields, "updated_at": now},
                )
            )

    async def clear(self) -> bool:
        async with self._db.session() as session, session.begin():
            result = await session.execute(
                delete(LoopStateModel).where(
                    self._db.scope.where(LoopStateModel), LoopStateModel.id == self._SINGLETON_ID
                )
            )
            return (result.rowcount or 0) > 0


# ── LoopProgressStore ────────────────────────────────────────────


class LoopProgressStore:
    """Per-(run_id, key) key/value rows.

    The model uses this to mark sections as completed across
    iterations — iteration N reads the rows iteration N-1 wrote
    so it can skip work that's already done. ``run_id`` scopes
    every operation so a fresh loop run can't accidentally see
    progress from an older run.

    All methods take ``run_id`` explicitly (rather than reading
    from :class:`Session`) so the store stays mockable in tests
    and the tool layer is responsible for current-run resolution.
    """

    # Reserved key that holds the agent's announced iteration total.
    # Read by :meth:`get_announced_total` (used by the ``/loop`` panel
    # header) and written by :meth:`set_announced_total` (used by the
    # ``loop_set_total`` agent tool). Underscored to keep it out of
    # the way of user-defined progress keys — callers should reach
    # for the typed accessors rather than the raw string.
    ANNOUNCED_TOTAL_KEY = "__loop_total__"

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

    async def get(self, run_id: str, key: str) -> str | None:
        async with self._db.session() as session:
            result = await session.execute(
                select(LoopProgressModel.value).where(
                    self._db.scope.where(LoopProgressModel),
                    LoopProgressModel.run_id == run_id,
                    LoopProgressModel.key == key,
                )
            )
            row = result.scalar_one_or_none()
            return row

    async def set(self, run_id: str, key: str, value: str) -> None:
        """Upsert a single (run_id, key) row.

        The model uses this on every "section verified" event;
        the row is keyed on (run_id, key) so calling ``set`` twice
        for the same section just updates the value (e.g. extra
        notes appended) rather than throwing on the unique
        constraint.
        """
        now = datetime.now()
        async with self._db.session() as session, session.begin():
            stmt = ConflictInsert.into(session, LoopProgressModel).values(
                **self._db.scope.stamp(),
                run_id=run_id,
                key=key,
                value=value,
                created_at=now,
                updated_at=now,
            )
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["org", "producer", "project", "run_id", "key"],
                    set_={"value": value, "updated_at": now},
                )
            )

    async def list(self, run_id: str) -> list[tuple[str, str]]:
        """Return all (key, value) pairs for ``run_id``, ordered
        by insertion time so iteration history reads chronologically."""
        async with self._db.session() as session:
            result = await session.execute(
                select(LoopProgressModel.key, LoopProgressModel.value)
                .where(self._db.scope.where(LoopProgressModel), LoopProgressModel.run_id == run_id)
                .order_by(LoopProgressModel.created_at)
            )
            return [(k, v) for k, v in result.all()]

    async def delete(self, run_id: str, key: str) -> bool:
        async with self._db.session() as session, session.begin():
            result = await session.execute(
                delete(LoopProgressModel).where(
                    self._db.scope.where(LoopProgressModel),
                    LoopProgressModel.run_id == run_id,
                    LoopProgressModel.key == key,
                )
            )
            return (result.rowcount or 0) > 0

    async def clear(self, run_id: str) -> int:
        """Delete every progress row for ``run_id``. Returns the
        deleted-row count so the caller can surface "cleared N
        progress entries" in chat."""
        async with self._db.session() as session, session.begin():
            result = await session.execute(
                delete(LoopProgressModel).where(
                    self._db.scope.where(LoopProgressModel), LoopProgressModel.run_id == run_id
                )
            )
            return result.rowcount or 0

    # ── Announced-total accessors (typed façade over the raw kv) ──

    async def get_announced_total(self, run_id: str) -> int | None:
        """Return the agent-announced iteration total for ``run_id``.

        Owned by the store rather than by the caller so the
        ``ANNOUNCED_TOTAL_KEY`` string + the ``int(...)`` fallback
        semantics have one home. Returns ``None`` when the key is
        unset or its value isn't parseable as an integer (silent
        fallback — the panel just hides the total in that case).
        """
        raw = await self.get(run_id, self.ANNOUNCED_TOTAL_KEY)
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    async def set_announced_total(self, run_id: str, total: int) -> None:
        """Persist the agent-announced iteration total for ``run_id``.

        Stringifies before writing (the underlying kv is string-typed)
        so the same value round-trips through
        :meth:`get_announced_total`.
        """
        await self.set(run_id, self.ANNOUNCED_TOTAL_KEY, str(total))
