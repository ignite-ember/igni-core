"""Which paths ``read_document`` may open: inside the project, and never a key or igni's own settings."""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path
from typing import ClassVar

from igni_core.core.documents.exceptions import DocumentRefused
from igni_core.core.documents.notes import DocumentNotes
from igni_core.core.hooks.bypass_rules import flag_file_read
from igni_core.core.paths import CONFIG_DIR, LEGACY_CONFIG_DIR


class DocumentAccess:
    """Resolves a path against the project and refuses what the model may not read."""

    _IGNI_DIRS: ClassVar[frozenset[str]] = frozenset({CONFIG_DIR, LEGACY_CONFIG_DIR})
    _IGNI_FILES: ClassVar[tuple[str, ...]] = (
        "config.yaml*",
        "config.yml*",
        "settings*.json",
        "credentials.json*",
    )
    """igni's settings and key stores, in a project's or the home config directory, backups included."""

    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir.expanduser().resolve()

    def check(self, raw: str) -> Path:
        """The file ``raw`` names, resolved; raises :class:`DocumentRefused` when it may not be read."""
        written = Path(raw).expanduser()
        if not written.is_absolute():
            written = self._project_dir / written
        resolved = written.resolve()
        if self._protected(written) or self._protected(resolved):
            raise DocumentRefused(DocumentNotes.protected(str(resolved)))
        if not resolved.is_relative_to(self._project_dir):
            raise DocumentRefused(DocumentNotes.outside_project(str(resolved)))
        if resolved.is_dir():
            raise DocumentRefused(DocumentNotes.directory(str(resolved)))
        if not resolved.exists():
            raise DocumentRefused(DocumentNotes.missing(str(resolved)))
        return resolved

    def _protected(self, path: Path) -> bool:
        if flag_file_read(str(path)) is not None:
            return True
        return path.parent.name in self._IGNI_DIRS and any(
            fnmatch(path.name, pattern) for pattern in self._IGNI_FILES
        )
