"""Per-client UI state.

The web client and the desktop app both need a
small amount of state that survives across reloads: which session a
window is bound to, sidebar collapsed/open, composer drafts, future
toggles. Each client stores nothing locally except a stable
``client_id``; everything else lives in this table — the desktop's
global ``igni.db``, the shared database in the cloud — so the
behaviour is identical regardless of the host.

The store is keyed by ``(client_id, key)`` within its scope — clients
enumerate their own state with a single ``get_for_client(client_id)``
call and write individual entries with ``set_value``. A window belongs
to its producer, not to one project, so the project column is empty.

**A failed operation says so.** Every method answers with a result
model: the operation happened, or the reason it did not. Nothing is
swallowed — a state that was not stored must not read as a state that
was, or the caller goes on to write over what is really there.

The reason a client is shown is a sentence about the store, never the
driver's own words: a failed read, a failed write, a failed delete.
What the database said about it stays in the log.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Integer, PrimaryKeyConstraint, String, Text, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from igni_core.core.db.base import Base
from igni_core.core.db.database import Database
from igni_core.core.db.locator import StorageLocator
from igni_core.core.db.scope import ScopedColumns
from igni_core.core.db.upsert import ConflictInsert

logger = logging.getLogger(__name__)

#: Why the store failed, in words a client may be shown. The driver's
#: own words stay in the log: they can carry SQL, a file name and a
#: user, and they go on the wire.
READ_FAILED = "the client state could not be read"
WRITE_FAILED = "the client state could not be written"
DELETE_FAILED = "the client state could not be removed"


class ClientStateModel(ScopedColumns, Base):
    __tablename__ = "ember_client_state"
    __table_args__ = (
        PrimaryKeyConstraint(
            "org", "producer", "project", "client_id", "key", name="pk_ember_client_state"
        ),
    )

    client_id: Mapped[str] = mapped_column(String, nullable=False)
    key: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class ClientStateResult(BaseModel):
    """What one operation on the store did — or why it could not.

    The Pattern-3 envelope: the caller is told, and decides what a lost
    state means for it.
    """

    model_config = ConfigDict(frozen=True)

    #: Whether the operation happened.
    ok: bool = True
    #: Why it did not, when it did not. A sentence a client may be
    #: shown: what went wrong with the store, never what the database
    #: said about it — a driver's message can carry SQL, a file name
    #: and a user. The traceback is in the log.
    error: str = ""


class ClientStateRead(ClientStateResult):
    """What reading a client's state produced: its values, or the error."""

    #: The client's stored values, by key. Empty when the read failed,
    #: and empty when the client has stored nothing — which is why the
    #: two are told apart by ``ok``.
    values: dict[str, str] = Field(default_factory=dict)


class ClientStateStoreProtocol(Protocol):
    """What reads and writes of a client's state need of a store.

    :class:`ClientStateStore` is the one over a database; a headless
    run's :class:`~igni_core.backend.headless_extensions.RunState` is
    the one that keeps what it is given for the length of the run.
    """

    def get_for_client(self, client_id: str, /) -> ClientStateRead: ...

    def set_value(self, client_id: str, key: str, value: str, /) -> ClientStateResult: ...

    def delete_value(self, client_id: str, key: str, /) -> ClientStateResult: ...


class ClientStateStore:
    """(client_id, key) → value KV store (global).

    The store over a database: it answers with a result, like every
    other one (:class:`ClientStateStoreProtocol`).
    """

    def __init__(self, db: Database | str | Path) -> None:
        self._db = Database.of(db)

    @classmethod
    def from_locator(cls, locator: StorageLocator) -> ClientStateStore:
        return cls(locator.global_database())

    def get_for_client(self, client_id: str) -> ClientStateRead:
        """Everything one client has stored.

        A read that fails does not come back as an empty state: an
        empty state reads as a client that has stored nothing, and the
        caller would go on to write over what is really there.
        """
        if not client_id:
            return ClientStateRead(ok=False, error="a client id is required")
        try:
            with self._db.sync_session() as session:
                rows = session.execute(
                    select(ClientStateModel.key, ClientStateModel.value).where(
                        self._db.scope.where(ClientStateModel),
                        ClientStateModel.client_id == client_id,
                    )
                ).all()
        except Exception:
            logger.exception("client state read failed for %r", client_id)
            return ClientStateRead(ok=False, error=READ_FAILED)
        return ClientStateRead(ok=True, values={key: value for key, value in rows})

    def set_value(self, client_id: str, key: str, value: str) -> ClientStateResult:
        """Store ``value`` under ``(client_id, key)``, replacing what was there."""
        if not client_id or not key:
            return ClientStateResult(ok=False, error="a client id and a key are required")
        now = int(time.time())
        try:
            with self._db.sync_session() as session, session.begin():
                stmt = ConflictInsert.into(session, ClientStateModel).values(
                    **self._db.scope.stamp(),
                    client_id=client_id,
                    key=key,
                    value=value,
                    updated_at=now,
                )
                session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["org", "producer", "project", "client_id", "key"],
                        set_={"value": value, "updated_at": now},
                    )
                )
        except Exception:
            logger.exception("client state write failed for %r/%r", client_id, key)
            return ClientStateResult(ok=False, error=WRITE_FAILED)
        return ClientStateResult(ok=True)

    def delete_value(self, client_id: str, key: str) -> ClientStateResult:
        """Remove ``(client_id, key)``. A key that was not there is not a failure."""
        if not client_id or not key:
            return ClientStateResult(ok=False, error="a client id and a key are required")
        try:
            with self._db.sync_session() as session, session.begin():
                session.execute(
                    delete(ClientStateModel).where(
                        self._db.scope.where(ClientStateModel),
                        ClientStateModel.client_id == client_id,
                        ClientStateModel.key == key,
                    )
                )
        except Exception:
            logger.exception("client state delete failed for %r/%r", client_id, key)
            return ClientStateResult(ok=False, error=DELETE_FAILED)
        return ClientStateResult(ok=True)


__all__ = [
    "DELETE_FAILED",
    "READ_FAILED",
    "WRITE_FAILED",
    "ClientStateModel",
    "ClientStateRead",
    "ClientStateResult",
    "ClientStateStore",
    "ClientStateStoreProtocol",
]
