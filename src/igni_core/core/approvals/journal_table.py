"""One table of the approval journal, reached best-effort.

A journal write that fails leaves the approval working in memory — it
just would not survive a restart — so a database error is an expected
outcome here, not an exception for the run: a write returns a
:class:`JournalWrite` saying whether it landed, a read returns nothing,
and both log the failure as a warning so a lost approval is visible.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from igni_core.core.db.database import Database

logger = logging.getLogger(__name__)

T = TypeVar("T")


class JournalWrite(BaseModel):
    """Whether a journal write landed; ``error`` says why when it did not."""

    ok: bool
    error: str = ""


class JournalTable:
    """A store over one journal table of ``db``, in ``db``'s scope."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def _write(self, what: str, change: Callable[[Session], object]) -> JournalWrite:
        try:
            with self._db.sync_session() as session, session.begin():
                change(session)
        except SQLAlchemyError as exc:
            logger.warning("approval journal: %s failed: %s", what, exc)
            return JournalWrite(ok=False, error=str(exc))
        return JournalWrite(ok=True)

    def _read(self, what: str, query: Callable[[Session], T], empty: T) -> T:
        try:
            with self._db.sync_session() as session:
                return query(session)
        except SQLAlchemyError as exc:
            logger.warning("approval journal: %s failed: %s", what, exc)
            return empty


__all__ = ["JournalTable", "JournalWrite"]
