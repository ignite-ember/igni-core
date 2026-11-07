"""A path an agent touched, as the project sees it — or not at all."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel


class ProjectPath(BaseModel):
    """A path relative to the session's project, or why there is none."""

    ok: bool
    #: POSIX, relative to the project root; set when ``ok``.
    relative: str = ""
    reason: str = ""


class ProjectDirs(Protocol):
    """Where each session's project is — :class:`~.backend.HostBackend` is one."""

    def project_dir_of(self, session_id: str) -> Path: ...

    def default_session(self) -> str: ...


class ProjectRoot:
    """Places a path inside the project of the session that touched it.

    ``dirs`` is asked each time, not captured: a test replaces the
    backend's methods under a running model.
    """

    def __init__(self, dirs: ProjectDirs) -> None:
        self._dirs = dirs

    def relative(self, session_id: str, path: str) -> ProjectPath:
        """Outside the project, the path is nothing an extension may be told."""
        if not path:
            return ProjectPath(ok=False, reason="no path")
        try:
            root = self._dirs.project_dir_of(session_id or self._dirs.default_session()).resolve()
            target = Path(path)
            target = target.resolve() if target.is_absolute() else (root / target).resolve()
        except (OSError, ValueError) as exc:
            # Not a path the filesystem can resolve (a null byte, a loop).
            return ProjectPath(ok=False, reason=str(exc))
        if not target.is_relative_to(root):
            return ProjectPath(ok=False, reason="outside the project")
        return ProjectPath(ok=True, relative=target.relative_to(root).as_posix())


__all__ = ["ProjectDirs", "ProjectPath", "ProjectRoot"]
