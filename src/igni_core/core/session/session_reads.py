"""When each chat last finished a run, and when the person last looked at it.

A chat whose run finished after the person last looked is **unread**: the
sidebar marks it until they open it. Kept beside the session → folder
registry, in the global database — the desktop's ``<data_dir>/igni.db``,
the shared database in the cloud — so what the person has seen follows
them from one window, device or VM to the next.

Times are Unix milliseconds: a run that finishes in the same second the
person looks must still be told apart.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import BigInteger, PrimaryKeyConstraint, String, or_, select
from sqlalchemy.orm import Mapped, mapped_column

from igni_core.core.db.base import Base
from igni_core.core.db.database import Database
from igni_core.core.db.locator import StorageLocator
from igni_core.core.db.scope import ScopedColumns
from igni_core.core.db.upsert import ConflictInsert

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class SessionReadModel(ScopedColumns, Base):
    __tablename__ = "ember_session_reads"
    # A session is in one project, so the project is not part of the key.
    __table_args__ = (
        PrimaryKeyConstraint("org", "producer", "session_id", name="pk_ember_session_reads"),
    )

    session_id: Mapped[str] = mapped_column(String, nullable=False)
    #: When a run in the chat last finished; ``None`` until one has.
    finished_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: When the person last looked at the chat; ``None`` until they have.
    seen_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class SessionReadStore:
    """Records finished runs and looks, and answers which chats are unread."""

    def __init__(self, db: Database | str | Path, clock: Callable[[], int] = _now_ms) -> None:
        self._db = Database.of(db)
        self._clock = clock

    @classmethod
    def from_locator(cls, locator: StorageLocator) -> SessionReadStore:
        return cls(locator.global_database())

    def finished(self, session_id: str, project_dir: str | Path) -> None:
        """A run in ``session_id`` finished just now."""
        self._stamp(session_id, project_dir, "finished_at")

    def seen(self, session_id: str, project_dir: str | Path) -> None:
        """The person looked at ``session_id`` just now."""
        self._stamp(session_id, project_dir, "seen_at")

    def unread(self) -> set[str]:
        """The chats whose last run finished after the person last looked."""
        model = SessionReadModel
        try:
            with self._db.sync_session() as session:
                rows = session.scalars(
                    select(model.session_id).where(
                        self._db.scope.tenant_where(model),
                        model.finished_at.is_not(None),
                        or_(model.seen_at.is_(None), model.finished_at > model.seen_at),
                    )
                )
                return set(rows)
        except Exception as exc:
            logger.debug("session reads: could not read the unread chats: %s", exc)
            return set()

    def _stamp(self, session_id: str, project_dir: str | Path, column: str) -> None:
        """Upsert. Best-effort — a failed write only leaves a mark wrong."""
        if not session_id:
            return
        scope = self._db.scope.for_project(project_dir)
        now = self._clock()
        try:
            with self._db.sync_session() as session, session.begin():
                stmt = ConflictInsert.into(session, SessionReadModel).values(
                    **scope.stamp(), session_id=session_id, **{column: now}
                )
                session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["org", "producer", "session_id"],
                        set_={"project": scope.project, column: now},
                    )
                )
        except Exception as exc:
            logger.debug("session reads: could not record %s for %s: %s", column, session_id, exc)


__all__ = ["SessionReadModel", "SessionReadStore"]
