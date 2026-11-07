"""What of one session's project an extension's workspace grant reaches.

A path is resolved against the project and held to the extension's
``workspacePaths``; a listing the backend returns is cut down to what
the grant reaches before the extension sees it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from igni_core.extensions.host.definition import OPENABLE_SUFFIXES
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.permissions import WorkspaceAccess


class WorkspaceScope:
    """One project root, seen through one extension's workspace grant."""

    def __init__(self, root: Path, access: WorkspaceAccess, extension_id: str) -> None:
        self.root = root
        self._access = access
        self._extension_id = extension_id

    def relative(self, path: str, *, directory: bool = False) -> str:
        """``path`` relative to the project, if the grant reaches it."""
        root = self.root.resolve()
        target = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        if not target.is_relative_to(root):
            raise HostModelError(ErrorCode.REFUSED, f"{path!r} is outside the project")
        relative = target.relative_to(root).as_posix()
        if relative == "." or not relative:
            if directory:
                return ""
            raise HostModelError(ErrorCode.INVALID, "a file path is required")
        if not directory and not self._access.permits(relative):
            raise HostModelError(
                ErrorCode.NOT_GRANTED,
                f"{relative!r} is outside '{self._extension_id}''s workspacePaths",
            )
        return relative

    def visible_dirs(self, relative: str, found: Any) -> Any:
        """``list_dirs``'s answer, with only the directories the grant reaches into."""
        if isinstance(found, dict) and isinstance(found.get("dirs"), list):
            prefix = f"{relative}/" if relative else ""
            found["dirs"] = [d for d in found["dirs"] if self._access.permits(f"{prefix}{d}/x")]
        return found

    def visible_completions(self, found: Any) -> Any:
        """``complete_files``'s answer, with only the paths the grant reaches."""
        if isinstance(found, dict):
            found["matches"] = [m for m in found.get("matches", []) if self._access.permits(str(m))]
            found["total"] = len(found["matches"])
        return found

    def visible_hits(self, found: Any) -> Any:
        """``search_code``'s answer, with only the hits in files the grant reaches."""
        if isinstance(found, dict):
            found["matches"] = [
                m for m in found.get("matches", []) if self._access.permits(str(m.get("path", "")))
            ]
        return found

    def document(self, relative: str) -> Path:
        """The file at ``relative``, when it is a document igni may open:
        a file, of a kind igni opens, and not executable."""
        target = self.root.resolve() / relative
        if not target.is_file():
            raise HostModelError(ErrorCode.NOT_FOUND, f"{relative!r} is not a file")
        if target.suffix.lower().lstrip(".") not in OPENABLE_SUFFIXES:
            raise HostModelError(ErrorCode.INVALID, f"{relative!r} is not a document igni opens")
        if os.access(target, os.X_OK):
            raise HostModelError(ErrorCode.INVALID, f"{relative!r} is executable")
        return target


__all__ = ["WorkspaceScope"]
