"""Which directory each session of the pool works in.

The record (:class:`SessionDirectoryStore`) outlives the process: a
session resumed tomorrow opens in the directory it was attached to
today. This owns that record together with the backend's own
directories, and the rule an attach is held to.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from igni_core.backend.schemas_pool import AttachRefused, ProjectDirs
from igni_core.backend.session_pool import SessionRuntime
from igni_core.core.session.session_directories import SessionDirectoryStore

logger = logging.getLogger(__name__)


class PoolDirectories:
    """The session-to-directory record, and the backend's own directories."""

    def __init__(self, store: SessionDirectoryStore, dirs: ProjectDirs) -> None:
        self._store = store
        self._dirs = dirs

    @property
    def project_dir(self) -> Path:
        """The directory the backend was started in."""
        return self._dirs.project_dir

    @property
    def additional_dirs(self) -> list[Path] | None:
        return self._dirs.additional_dirs

    def dir_for(self, session_id: str) -> Path:
        """The session's own directory, if it ever had one that still exists;
        otherwise the backend's."""
        recorded = Path(self._store.get_dir(session_id) or self.project_dir)
        if recorded.is_dir():
            return recorded
        logger.warning(
            "session %s dir %s missing; falling back to %s",
            session_id,
            recorded,
            self.project_dir,
        )
        return self.project_dir

    def record(self, session_id: str, project_dir: str | Path) -> None:
        self._store.set_dir(session_id, project_dir)

    def record_if_changed(self, runtime: SessionRuntime) -> None:
        """Record the runtime's directory when its id or directory moved since
        the last time; a repeat would open the database on every message."""
        if runtime.record_dir_registered(runtime.current_session_id(), runtime.backend.project_dir):
            self.record(runtime.backend.session_id, runtime.backend.project_dir)

    def claim(self, session_id: str, project_dir: str | None) -> str | AttachRefused:
        """The session an attach gets, or why it is refused.

        With a directory, the session is recorded there before it is
        created, so the runtime opens in it; a new one gets an id, and an
        existing one recorded in another directory is refused. With an
        id alone, the caller is restoring a session it was bound to (a
        window reloading), and a session of another project is refused:
        opening it would read that project's database inside this backend.
        With neither, a new session in the backend's own directory.
        """
        if project_dir:
            wanted = Path(project_dir).expanduser()
            if not wanted.is_dir():
                return AttachRefused(reason=f"not a directory: {project_dir}")
            wanted = wanted.resolve()
            if session_id:
                existing = self._store.get_dir(session_id)
                if existing and Path(existing).resolve() != wanted:
                    # Recording it here would move the session to another
                    # folder: its history would be read from one project and
                    # its tools run in another.
                    return AttachRefused(
                        reason=f"session {session_id} belongs to {existing}, not to {project_dir}"
                    )
            session_id = session_id or self._new_id()
            self.record(session_id, wanted)
            return session_id
        if not session_id:
            return self._new_id()
        existing = self._store.get_dir(session_id)
        if existing and Path(existing).resolve() != self.project_dir.resolve():
            return AttachRefused(
                reason=f"session {session_id} belongs to a different project "
                f"({existing}); this backend is bound to {self.project_dir}"
            )
        return session_id

    @staticmethod
    def _new_id() -> str:
        return str(uuid.uuid4())[:8]
