"""A path an agent touched, as the project sees it — or not at all."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel


class ProjectPath(BaseModel):
    """A path relative to the session's project, or why there is none."""

    ok: bool
    #: POSIX, relative to the project root; set when ``ok``.
    relative: str = ""
    reason: str = ""


class ProjectRoot:
    """Places a path inside the project of the session that touched it."""

    def __init__(
        self, project_dir_of: Callable[[str], Path], default_session: Callable[[], str]
    ) -> None:
        self._project_dir_of = project_dir_of
        self._default_session = default_session

    def relative(self, session_id: str, path: str) -> ProjectPath:
        """Outside the project, the path is nothing an extension may be told."""
        if not path:
            return ProjectPath(ok=False, reason="no path")
        try:
            root = self._project_dir_of(session_id or self._default_session()).resolve()
            target = Path(path)
            target = target.resolve() if target.is_absolute() else (root / target).resolve()
        except (OSError, ValueError) as exc:
            # Not a path the filesystem can resolve (a null byte, a loop).
            return ProjectPath(ok=False, reason=str(exc))
        if not target.is_relative_to(root):
            return ProjectPath(ok=False, reason="outside the project")
        return ProjectPath(ok=True, relative=target.relative_to(root).as_posix())


__all__ = ["ProjectPath", "ProjectRoot"]
