"""When each chat last had a message, finished a run, and was looked at.

A chat whose run finished after the person last looked is **unread**: the
sidebar marks it until they open it. Chats are listed by their last
message — a message going in or a reply coming out — so the newest
conversation is on top the moment it moves. Kept beside the session → folder
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

from sqlalchemy import BigInteger, PrimaryKeyConstraint, String, select
from sqlalchemy.orm import Mapped, mapped_column

from igni_core.core.db.base import Base
from igni_core.core.db.database import Database
from igni_core.core.db.locator import StorageLocator
from igni_core.core.db.scope import ScopedColumns
from igni_core.core.db.upsert import ConflictInsert
from igni_core.core.session.schemas import SessionReadRow

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
    #: When a message last went in or a reply came out; ``None`` until one has.
    last_message_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class SessionReadStore:
    """Records finished runs and looks, and answers which chats are unread."""

    def __init__(self, db: Database | str | Path, clock: Callable[[], int] = _now_ms) -> None:
        self._db = Database.of(db)
        self._clock = clock

    @classmethod
    def from_locator(cls, locator: StorageLocator) -> SessionReadStore:
        return cls(locator.global_database())

    def message(self, session_id: str, project_dir: str | Path) -> None:
        """A message went into ``session_id`` just now: a run started."""
        self._stamp(session_id, project_dir, "last_message_at")

    def finished(self, session_id: str, project_dir: str | Path) -> None:
        """A run in ``session_id`` finished just now: its reply is the last message."""
        self._stamp(session_id, project_dir, "finished_at", "last_message_at")

    def seen(self, session_id: str, project_dir: str | Path) -> None:
        """The person looked at ``session_id`` just now."""
        self._stamp(session_id, project_dir, "seen_at")

    def unread(self) -> set[str]:
        """The chats whose last run finished after the person last looked."""
        return {row.session_id for row in self.rows() if row.unread}

    def rows(self) -> list[SessionReadRow]:
        """Every chat recorded: whether it is unread, and when its last message was."""
        model = SessionReadModel
        try:
            with self._db.sync_session() as session:
                found = session.execute(
                    select(
                        model.session_id, model.finished_at, model.seen_at, model.last_message_at
                    ).where(self._db.scope.tenant_where(model))
                ).all()
        except Exception as exc:
            logger.debug("session reads: could not read the chats' marks: %s", exc)
            return []
        return [
            SessionReadRow(
                session_id=session_id,
                unread=finished is not None and (seen is None or finished > seen),
                last_message_at=last or 0,
            )
            for session_id, finished, seen, last in found
        ]

    def _stamp(self, session_id: str, project_dir: str | Path, *columns: str) -> None:
        """Upsert. Best-effort — a failed write only leaves a mark wrong."""
        if not session_id:
            return
        scope = self._db.scope.for_project(project_dir)
        now = self._clock()
        stamped = dict.fromkeys(columns, now)
        try:
            with self._db.sync_session() as session, session.begin():
                stmt = ConflictInsert.into(session, SessionReadModel).values(
                    **scope.stamp(), session_id=session_id, **stamped
                )
                session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["org", "producer", "session_id"],
                        set_={"project": scope.project, **stamped},
                    )
                )
        except Exception as exc:
            logger.debug("session reads: could not record %s for %s: %s", columns, session_id, exc)


__all__ = ["SessionReadModel", "SessionReadStore"]
