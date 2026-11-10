"""Where one project's memory files live.

A project's memory bank has two possible homes: igni's own
(``~/.igni/projects/<slug>/memory/``) and Claude Code's
(``~/.claude/projects/<slug>/memory/``), the second read-only and
used as a cross-tool fallback. The rules for deriving them — the slug
encoding, the two base directories — are the same for every project,
so they live here once, as state on :class:`ProjectMemoryPaths`
rather than as free functions that each take the project directory
as their first argument.

:class:`MemoryPaths` is the shape :class:`~igni_core.core.utils.context_memory.ProjectMemoryBank`
depends on. A caller that keeps its memory somewhere else — a test
with a sandbox, a deployment with its own layout — supplies its own
implementation instead of patching a module global.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from igni_core.core.paths import CONFIG_DIR


class MemoryPaths(Protocol):
    """The two directories a project's memory files can live in."""

    def igni_dir(self) -> Path:
        """The project's own memory directory — the one igni writes to."""

    def claude_dir(self) -> Path:
        """The Claude Code memory directory for the same project, read only."""


@dataclass(frozen=True)
class ProjectMemoryPaths:
    """The memory directories of one project, derived from its path.

    The project directory is the subject: the slug is its encoded
    form, and both directories follow from the slug. Claude Code's
    convention is mirrored exactly — the absolute path with every
    ``/`` replaced by ``-`` — so a user who already has a Claude Code
    memory bank for this repo lights up the cross-tool fallback with
    no migration step.
    """

    project_dir: Path

    @property
    def slug(self) -> str:
        """The project path as a directory-safe slug.

        ``/Users/x/proj`` becomes ``-Users-x-proj``: the absolute path
        with every ``/`` replaced by ``-``, so the leading ``/``
        becomes a leading ``-``.
        """
        return str(self.project_dir.resolve()).replace("/", "-")

    def igni_dir(self) -> Path:
        """igni's own per-project memory dir
        (``~/.igni/projects/<slug>/memory/``)."""
        return Path.home() / CONFIG_DIR / "projects" / self.slug / "memory"

    def claude_dir(self) -> Path:
        """Claude Code's per-project memory dir
        (``~/.claude/projects/<slug>/memory/``). Read only — we never
        write here."""
        return Path.home() / ".claude" / "projects" / self.slug / "memory"


__all__ = ["MemoryPaths", "ProjectMemoryPaths"]
