"""Thin facade over the persistence stores.

:class:`SessionPersistence` owns one instance each of the six
coordinators built on top of a shared :class:`SessionDataService`
and forwards the public methods (``list_sessions`` / ``auto_name`` /
``rename`` / ``title`` / ``fork`` / ``get_name`` / ``_get_name_raw`` /
``load_plan_decisions`` / ``load_todos`` / ``load_event_log`` /
``save_event_log`` / ``save_todos`` / ``save_plan_decisions``) to
the store that owns each concern.

The facade is the API boundary, so the shapes that cross it are
TYPED end to end (Rule 1) and FAILURES are REPORTED, not raised
(Pattern 3):

* loads return the typed model the caller needs — a
  :class:`SessionListRow` list, a :class:`PlanDecisionsBlob`, a
  ``list[TodoItemWire]``, a ``list[SessionEvent]``;
* writes return the store's :class:`PersistResult` so a failed
  write is visible to the caller instead of being swallowed;
* :meth:`fork` returns the store's :class:`ForkResult` — the
  caller decides what a failed fork means rather than the facade
  raising on its behalf.

There are no legacy ``dict`` shapes on this boundary: the stores
own the wire coercion (they dump a model to the ``dict`` Agno's
``session_data`` column stores) and the domain stays typed on the
way in and out.

Rule 6 (OOP): the god-class from the pre-refactor
``persistence.py`` dissolves into six ~30-70 LoC coordinators +
one ~90 LoC service; this facade is the assembly point that
threads them together and preserves the public API surface.
"""

from __future__ import annotations

import logging
from typing import Any

from igni_core.core.session.event_log_schema import SessionEvent
from igni_core.core.session.persistence.data_service import SessionDataService
from igni_core.core.session.persistence.db_protocol import AgnoSessionDb
from igni_core.core.session.persistence.event_log_store import EventLogStore
from igni_core.core.session.persistence.forker import SessionForker
from igni_core.core.session.persistence.listing import SessionListing
from igni_core.core.session.persistence.namer import SessionNamer
from igni_core.core.session.persistence.plan_decisions_store import (
    PlanDecisionsStore,
)
from igni_core.core.session.persistence.todos_store import TodoSnapshotStore
from igni_core.core.session.schemas import (
    ForkResult,
    PersistResult,
    SessionListRow,
)
from igni_core.core.tools.plan import PlanDecisionsBlob
from igni_core.core.tools.todo import TodoItemWire

logger = logging.getLogger(__name__)


class SessionPersistence:
    """Handles session listing, naming, and metadata via Agno's DB.

    Thin facade over six store coordinators — each owns one
    concern (listing / naming / forking / plan-decisions / todos /
    event-log) and composes a shared :class:`SessionDataService`
    for the read/write chokepoint against ``session_data``.

    Every public method is forwarded to the appropriate store; the
    typed models and the Pattern-3 envelopes the stores return
    reach the caller unchanged.
    """

    def __init__(self, db: AgnoSessionDb | None, session_id: str) -> None:
        # ``db`` is the narrow :class:`AgnoSessionDb` protocol — the
        # four coroutines the persistence layer actually calls into.
        # ``None`` means persistence is disabled (headless CLI, tests
        # without a DB); every store treats that as a no-op.
        self._db: AgnoSessionDb | None = db
        self._session_id = session_id
        self._data = SessionDataService(db, session_id)
        self._listing = SessionListing(db)
        self._namer = SessionNamer(db, session_id, self._data)
        self._forker = SessionForker(db, session_id)
        self._plan_store = PlanDecisionsStore(self._data)
        self._todos_store = TodoSnapshotStore(self._data)
        self._event_log_store = EventLogStore(self._data)

    # ── Legacy attribute exposure ──────────────────────────────
    #
    # Existing callers reach for ``.db`` / ``.session_id`` as
    # public attributes (e.g. :class:`SessionIdentity` mutates
    # ``.session_id`` during a rotate). The properties preserve
    # that surface and thread mutations through to the underlying
    # stores.

    @property
    def db(self) -> AgnoSessionDb | None:
        return self._db

    @property
    def session_id(self) -> str:
        return self._session_id

    @session_id.setter
    def session_id(self, new_id: str) -> None:
        """Retarget every store at the new session id.

        Mirrors the pre-refactor ``self.session_id = new_id``
        write that :class:`SessionIdentity._rotate_id` performs
        after a fork — the previous god-class stored a single
        string, this facade fans the change out to every store.
        """
        self._session_id = new_id
        self._data.session_id = new_id
        self._namer.rebind(new_id)
        self._forker.rebind(new_id)

    # ── Session listing ────────────────────────────────────────

    async def list_sessions(self, limit: int | None = None) -> list[SessionListRow]:
        """List sessions from the Agno database.

        Returns typed :class:`SessionListRow` models — the caller
        reaches for named attributes instead of dict keys, and the
        wire boundary (where a row becomes JSON) is the one place
        that dumps. On DB failure the facade preserves the
        pre-refactor "swallow-and-default-to-empty" contract.
        """
        result = await self._listing.list_sessions(limit=limit)
        return result.value or []

    # ── Session name ───────────────────────────────────────────

    async def auto_name(self, model: Any, *, model_busy: bool = False) -> str:
        """Title the session from its first message(s) — see
        :meth:`SessionNamer.auto_name`. Returns the title, or ``""``."""
        return await self._namer.auto_name(model, model_busy=model_busy)

    async def rename(self, new_name: str) -> PersistResult:
        """Manually rename the current session.

        Returns the namer's :class:`PersistResult` instead of
        discarding it. Swallowing it here is what let ``/rename``
        answer "Session renamed to: X" after a failed write — the DB
        error was logged at DEBUG and the caller had no way to know.
        Callers that ignore the return value behave exactly as before.
        """
        return await self._namer.rename(new_name)

    async def title(self, name: str) -> PersistResult:
        """Name the session, creating its row if it has none yet."""
        return await self._namer.title(name)

    async def get_name(self) -> str:
        """Get the current session's name from the database."""
        return await self._namer.get_name()

    async def _get_name_raw(self) -> str:
        """Read the persisted session name verbatim (no cleanup).

        Underscore-prefixed on the facade — historic private API
        that :class:`SessionNamer.auto_name` calls internally.
        Kept exposed here for tests that exercise the naming
        pipeline at the persistence layer directly.
        """
        return await self._namer._get_name_raw()

    # ── Fork ──────────────────────────────────────────────────

    async def fork(self, name: str | None = None) -> ForkResult:
        """Clone the current session under a fresh ``session_id``.

        Returns the forker's :class:`ForkResult` — ``ok=True`` with
        the new 8-char ``new_session_id`` on success, ``ok=False``
        with a diagnostic ``error`` otherwise. The caller decides
        what a failed fork means (the ``/fork`` slash command turns
        it into an error message; nothing else forks today).
        """
        return await self._forker.fork(name=name)

    # ── Plan decisions ─────────────────────────────────────────

    async def load_plan_decisions(self) -> PlanDecisionsBlob:
        """Read the persisted ``{run_id: "approved"|"dismissed"}``
        map as a typed :class:`PlanDecisionsBlob`.

        An empty blob means "no decision recorded" — the safe
        pending default. A corrupt row is filtered by the blob's own
        tolerant constructor, and a DB failure yields an empty blob
        too, so a load never raises.
        """
        result = await self._plan_store.load()
        return result.value or PlanDecisionsBlob()

    async def save_plan_decisions(self, blob: PlanDecisionsBlob) -> PersistResult:
        """Write the ``{run_id: decision}`` map to
        ``session_data['plan_decisions']``.

        Takes the typed :class:`PlanDecisionsBlob` — the store
        materialises the plain ``dict[str, str]`` on-disk shape
        itself, so the on-disk contract stays where it belongs.

        Returns the store's :class:`PersistResult`. The
        persist-then-flip invariant in :class:`PlanCoordinator` is
        honoured by the caller keying on ``ok`` — the approve path
        aborts the mode flip when the write did not land.
        """
        return await self._plan_store.save(blob)

    # ── Todos ─────────────────────────────────────────────────

    async def load_todos(self) -> list[TodoItemWire]:
        """Read the persisted todo snapshot for this session.

        Returns typed :class:`TodoItemWire` rows. Malformed rows
        drop silently at the store's coercion step, so the list
        always holds well-formed rows.
        """
        result = await self._todos_store.load()
        return result.value or []

    async def save_todos(self, todos: list[TodoItemWire]) -> PersistResult:
        """Atomic-replace persisted todo snapshot.

        Takes typed :class:`TodoItemWire` rows — the store dumps
        them at the SQL boundary. Returns the store's
        :class:`PersistResult` so a caller that cares about a lost
        write can see it; callers that don't are unaffected.
        """
        return await self._todos_store.save(todos)

    # ── Event log ────────────────────────────────────────────

    async def load_event_log(self) -> list[SessionEvent]:
        """Read the persisted append-only event log.

        Returns typed :class:`SessionEvent` entries — the same
        union the in-memory log holds, so a caller reads named
        attributes instead of dict keys. Every persisted entry has
        been round-tripped through the store's adapter — stale rows
        drop there, so the output comes from one Pydantic definition
        (Rule 1).
        """
        result = await self._event_log_store.load()
        return result.value or []

    async def save_event_log(self, events: list[SessionEvent]) -> PersistResult:
        """Atomic-replace the session's event log.

        Callers pass typed :class:`SessionEvent` entries; the store
        dumps them to wire dicts at its own SQL boundary, so the
        domain stays typed to the boundary (Rule 1).

        The store's :class:`PersistResult` is returned as-is: a
        failed write is reported, not raised, so the caller can
        decide what a lost event means.
        """
        return await self._event_log_store.save(events)
