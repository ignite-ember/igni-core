"""Session persistence — split from the pre-refactor 534-LoC god
module into a package of small, single-purpose coordinators.

Public re-exports:

* :class:`SessionPersistence` — the facade every existing caller
  imports; forwards to the six stores below and preserves the
  historic dict / list / ``None``-return shapes at the public
  boundary.
* :class:`SessionDataService` — the shared read/write chokepoint
  over Agno's ``session_data`` blob (owns the lock, the
  create-if-missing branch, the merge-and-upsert flow).
* :class:`AgnoSessionDb` — runtime-checkable protocol closing the
  ``db: Any`` seam.
* :class:`ScopedAgnoDb` — runtime-checkable protocol for the scope a
  *shared* database's storage fixes Agno's ``user_id`` to.
* :class:`SessionListing` / :class:`SessionNamer` /
  :class:`SessionForker` / :class:`PlanDecisionsStore` /
  :class:`TodoSnapshotStore` / :class:`EventLogStore` — the six
  store coordinators; tests can construct one in isolation
  without wiring up the full facade.
"""

from igni_core.core.session.persistence.data_service import SessionDataService
from igni_core.core.session.persistence.db_protocol import AgnoSessionDb, ScopedAgnoDb
from igni_core.core.session.persistence.event_log_store import EventLogStore
from igni_core.core.session.persistence.facade import SessionPersistence
from igni_core.core.session.persistence.forker import SessionForker
from igni_core.core.session.persistence.listing import SessionListing
from igni_core.core.session.persistence.namer import SessionNamer
from igni_core.core.session.persistence.plan_decisions_store import (
    PlanDecisionsStore,
)
from igni_core.core.session.persistence.todos_store import TodoSnapshotStore

__all__ = [
    "AgnoSessionDb",
    "EventLogStore",
    "PlanDecisionsStore",
    "ScopedAgnoDb",
    "SessionDataService",
    "SessionForker",
    "SessionListing",
    "SessionNamer",
    "SessionPersistence",
    "TodoSnapshotStore",
]
