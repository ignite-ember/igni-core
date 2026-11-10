"""Typed protocol for Agno's session DB — closes the ``db: Any``
seam that used to live on :class:`SessionPersistence`.

Every persistence store composes an :class:`AgnoSessionDb`
instance instead of accepting an untyped ``Any``. The protocol
declares only the four methods the persistence layer actually
uses (``get_session`` / ``upsert_session`` / ``rename_session`` /
``get_sessions``) — the wider Agno DB surface is out of scope.

Marked ``@runtime_checkable`` so tests can smoke-check
``isinstance(real_agno_db, AgnoSessionDb)`` to catch drift
between this hand-written protocol and Agno's actual signatures.

:class:`ScopedAgnoDb` is the other half of the same contract: what a
*shared* database's storage adds on top — the scope it fixes Agno's
``user_id`` to, so every row the session writes is found again by it.
The desktop's storage has no such field, which is why the protocol
is a question rather than a cast.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ScopedAgnoDb(Protocol):
    """An Agno database whose rows are fixed to one scope.

    In the cloud the storage behind the session's ``db`` is scoped: it
    fixes Agno's ``user_id`` to the session's scope, so the rows of one
    producer are the rows of nobody else. The desktop's storage has no
    scope of its own — its file is the boundary.
    """

    @property
    def scoped_user_id(self) -> str:
        """The scope Agno's ``user_id`` is fixed to."""
        ...


@runtime_checkable
class AgnoSessionDb(Protocol):
    """Minimum surface the persistence stores require from the
    Agno session-DB layer.

    Kept intentionally narrow: any Agno DB implementation (the
    concrete SQLite / Postgres backends, plus test doubles) that
    exposes these four coroutines satisfies the contract. The
    stores never reach for other attributes on this object.
    """

    async def get_session(
        self,
        *,
        session_id: str,
        session_type: Any,
        deserialize: bool = True,
    ) -> Any:
        """Load one session row by id. Returns ``None`` on miss."""
        ...

    async def upsert_session(self, session: Any, *, deserialize: bool = True) -> Any:
        """Insert or update a session row (keyed by ``session_id``)."""
        ...

    async def rename_session(
        self,
        *,
        session_id: str,
        session_type: Any,
        session_name: str,
    ) -> Any:
        """Update the ``session_data["session_name"]`` field for one
        session row."""
        ...

    async def get_sessions(
        self,
        *,
        session_type: Any,
        limit: int | None = None,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
        deserialize: bool = True,
    ) -> Any:
        """List session rows (returns a list or ``(list, count)``
        tuple depending on the concrete backend)."""
        ...
