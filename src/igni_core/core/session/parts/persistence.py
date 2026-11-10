"""What the session persists, and what it learns.

The database holds the session's rows; the persister writes the
session's own state into them; the memory manager reads and writes the
person's long-term memory; the learning manager extracts what a turn
taught and injects it back into the next one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from igni_core.core.learn import create_learning_machine
from igni_core.core.memory.manager import StorageManager
from igni_core.core.session.learning_ops import SessionLearningManager
from igni_core.core.session.memory_ops import SessionMemoryManager
from igni_core.core.session.persistence import SessionPersistence

if TYPE_CHECKING:
    from igni_core.core.session.core import Session


class PersistenceBuilder:
    """The database, the persister, the memory manager and the learning manager."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def build_database(self) -> None:
        """The Agno database the session's rows live in."""
        session = self._session
        session.db = StorageManager.build_db(session.settings, project_dir=session.project_dir)

    def build_persistence(self) -> None:
        """The persister, the memory manager and the learning manager."""
        session = self._session
        session.persistence = SessionPersistence(session.db, session.session_id)
        session.memory_mgr = SessionMemoryManager(session.db, session.settings, session.user_id)

        boot = create_learning_machine(session.settings, session.db)
        session.learning_mgr = SessionLearningManager(
            settings=session.settings,
            db=session.db,
            user_id_ref=lambda: session.user_id,
            session_id_ref=lambda: session.session_id,
            main_team_ref=lambda: session.main_team,
            learning=boot.machine,
            boot_reason=boot.reason,
        )


__all__ = ["PersistenceBuilder"]
