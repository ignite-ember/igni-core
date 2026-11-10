"""Per-project auto-memory — MEMORY.md index loading + write-back
instructions the agent needs at session start.

Extracted from :mod:`igni_core.core.utils.context` per
CODE_STANDARDS.md Pattern 8 (small modules, one responsibility). The
parent module was 778 LoC of hierarchical rules loading with
memory-index handling glued into the middle; this file holds the
memory-index concern only.

## OOP shape

The subject of this file is a project's memory bank: where its files
live, what its index says, and what the agent is told about writing
back into it. :class:`ProjectMemoryBank` owns that subject — the
project directory is constructor state, and the three public verbs
(``ensure`` / ``load_index`` / ``writeback_instructions``) are its
methods. There are no free-function delegations back to it.

Where the bank keeps its files is a collaborator
(:class:`~igni_core.core.utils.context_memory_paths.MemoryPaths`),
passed in or built from the project directory. That is the seam a
caller with a different layout uses — nothing in the bank reads a
module global to find out where memory lives.

## What lives here

- **``MEMORY.md`` reading** — with the 200-line / 25-KB cap Claude Code
  publishes, so a runaway memory file can never blow up the session
  prompt.
- **Write-back instructions** — the system-prompt block the agent
  sees at session start, delegated to
  :class:`context_memory_prompt.MemoryWritebackPrompt` so the 79-line
  template lives in its own file (Pattern 8).
- **Bootstrap** — :meth:`ProjectMemoryBank.ensure` creates the per-
  project memory directory idempotently so the agent's first
  ``save_file`` doesn't fail on a missing parent.
"""

from __future__ import annotations

import logging
from pathlib import Path

from igni_core.core.utils.context_memory_paths import MemoryPaths, ProjectMemoryPaths
from igni_core.core.utils.context_memory_prompt import MemoryWritebackPrompt
from igni_core.core.utils.memory_files import DirectoryMemoryFiles, MemoryFiles, MemoryMirror

logger = logging.getLogger(__name__)


# ── The bank ──────────────────────────────────────────────────────


class ProjectMemoryBank:
    """One project's auto-memory bank — the OOP core of this module.

    Owns a ``project_dir``, the paths that follow from it, and a
    cross-tool fallback flag; exposes the three verbs the caller
    actually cares about (``ensure`` / ``load_index`` /
    ``writeback_instructions``) as methods. Replaces the pre-refactor
    free-function trio that threaded ``project_dir`` through five
    signatures as an implicit subject (Rule 6 "implicit subject
    cluster" audit flag).
    """

    #: Filename of the per-project memory INDEX — the index is a
    #: one-line-per-memory table of contents; the individual memory
    #: files are separate ``<name>.md`` files beside it.
    MEMORY_INDEX_NAME: str = "MEMORY.md"

    #: Prefix line cap Claude Code publishes. Loaded into the session
    #: prompt at start; the rest of the file survives untouched but is
    #: not visible until the agent explicitly reads it.
    MAX_LINES: int = 200

    #: Byte cap that runs alongside the line cap — whichever hits
    #: first wins. Matches Claude Code's 25 KB published cap.
    MAX_BYTES: int = 25_000

    def __init__(
        self,
        project_dir: Path,
        read_claude_fallback: bool = True,
        files: MemoryFiles | None = None,
        paths: MemoryPaths | None = None,
    ) -> None:
        self._project_dir = project_dir
        self._read_claude_fallback = read_claude_fallback
        self._files = files
        self._paths: MemoryPaths = paths if paths is not None else ProjectMemoryPaths(project_dir)

    def mirror(self) -> MemoryMirror | None:
        """The write-through mirror of the memory directory, filled —
        in the cloud. ``None`` on the desktop, where the files are the
        store. See :mod:`igni_core.core.utils.memory_files`."""
        files = self.files()
        if isinstance(files, DirectoryMemoryFiles):
            return None
        mirror = MemoryMirror(files, self.igni_dir())
        try:
            mirror.fill()
        except OSError as exc:
            logger.warning("memory files: could not fill %s: %s", mirror.directory, exc)
        return mirror

    def files(self) -> MemoryFiles:
        """Where this project's memory files are: :meth:`igni_dir` on the
        desktop, the shared database in the cloud (:class:`MemoryFiles`)."""
        if self._files is None:
            self._files = MemoryFiles.for_project(self._project_dir, self.igni_dir())
        return self._files

    # ── Path resolution ─────────────────────────────────────────

    @property
    def project_dir(self) -> Path:
        return self._project_dir

    @property
    def read_claude_fallback(self) -> bool:
        return self._read_claude_fallback

    def igni_dir(self) -> Path:
        """Resolve the igni-owned memory dir for this project."""
        return self._paths.igni_dir()

    def claude_dir(self) -> Path:
        """Resolve the Claude Code memory dir for this project."""
        return self._paths.claude_dir()

    # ── Verbs ───────────────────────────────────────────────────

    def ensure(self) -> Path:
        """Create the per-project memory directory if missing and
        return its path. Idempotent — existing directories are left
        alone, and any OS-level permission error is logged +
        swallowed so a flaky disk doesn't break session boot.
        """
        target = self.igni_dir()
        if not isinstance(self.files(), DirectoryMemoryFiles):
            # The cloud: the directory is the database's, filled by
            # :meth:`mirror` — nothing to create here.
            return target
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("memory dir %s could not be created: %s", target, exc)
        return target

    def load_index(self) -> str:
        """Load the per-project ``MEMORY.md`` index for the session.

        Tries the igni-owned location first; when
        ``read_claude_fallback`` is set and no igni-owned file
        exists, falls back to the equivalent CC path so a user
        mid-migration keeps their existing memory bank without
        copying files. The first 200 lines OR 25 KB (whichever cap
        hits first) load into context — the same prefix budget
        Claude Code applies — so a runaway memory file can never
        blow up the session prompt.

        In the cloud the index is a row of the shared database, and
        there is no Claude Code directory to fall back to.
        """
        files = self.files()
        if not isinstance(files, DirectoryMemoryFiles):
            return self.cap_index(files.read(self.MEMORY_INDEX_NAME) or "")
        text = self._index_at(self.igni_dir())
        if text:
            return text
        if self._read_claude_fallback:
            text = self._index_at(self.claude_dir())
            if text:
                return text
        return ""

    def writeback_instructions(self) -> str:
        """Return the system-prompt block that teaches the agent
        how to WRITE new memory entries during a conversation
        (Claude Code parity, row 61).

        Delegates the actual template rendering to
        :class:`context_memory_prompt.MemoryWritebackPrompt` so the
        79-line prose lives in its own file (Pattern 8). This
        method's job is to wire the resolved igni-dir path into
        the prompt.
        """
        return MemoryWritebackPrompt(self.igni_dir()).render()

    # ── Reading ────────────────────────────────────────────────

    def _index_at(self, memory_dir: Path) -> str:
        """The index text in one memory directory, capped."""
        files = DirectoryMemoryFiles(memory_dir)
        return self.cap_index(files.read(self.MEMORY_INDEX_NAME) or "")

    @classmethod
    def cap_index(cls, content: str) -> str:
        """The first 200 lines or 25 KB of an index, whichever is less.

        The line cap comes first; the byte cap then trims the
        trailing partial text. ``errors="ignore"`` drops any byte
        chopped mid-codepoint so we never emit invalid UTF-8.
        """
        lines = content.splitlines(keepends=True)
        if len(lines) > cls.MAX_LINES:
            lines = lines[: cls.MAX_LINES]
        text = "".join(lines)
        if len(text.encode("utf-8")) > cls.MAX_BYTES:
            text = text.encode("utf-8")[: cls.MAX_BYTES].decode("utf-8", errors="ignore")
        return text
