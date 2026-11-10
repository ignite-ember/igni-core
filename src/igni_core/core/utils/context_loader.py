"""The rules tiers the rules-context loader drives.

:mod:`context` owns the coordinator — :class:`RulesContextLoader`, the
class that holds a session's rules state and walks the tiers in order.
This module holds the strategies it drives: the :class:`RulesReaders`
composition (the file-read helpers every tier consumes), the
:class:`RulesTier` protocol, and one class per tier.

## Why this shape

* One class per tier means adding a tier is one class plus one entry
  in the loader's ``tiers()`` — no dispatch dict to keep in sync.
* Every tier owns what it reads: the reader helpers and the paths it
  reads from. A tier does its own loading and never calls back into
  the loader, so a tier can be built and used on its own.
* The readers arrive as one typed composition, so the tier signatures
  stay free of ``Callable[..., str]`` parameters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from igni_core.core.utils.context_managed import load_managed_rules
from igni_core.core.utils.context_memory import ProjectMemoryBank
from igni_core.core.utils.context_memory_paths import MemoryPaths
from igni_core.core.utils.context_project import (
    load_project_rules,
    load_project_rules_dirs,
    load_subdirectory_rules,
)
from igni_core.core.utils.context_readers import (
    read_if_exists,
    read_rules_dir,
    read_rules_dir_files,
    read_with_imports,
    rules_filenames,
)
from igni_core.core.utils.context_schemas import (
    ReadIfExists,
    ReadRulesDir,
    ReadRulesDirFiles,
    ReadWithImports,
    RulesFilenames,
    SubdirectoryRules,
    UserRulesPaths,
)
from igni_core.core.utils.context_user import load_user_rules

# ── Reader composition ─────────────────────────────────────────────


@dataclass(frozen=True)
class RulesReaders:
    """The file-read helpers every rules tier consumes, as one value.

    Bundling the readers into one object keeps the ``Callable[..., str]``
    soup out of the tier signatures: each helper is typed by a Protocol
    in :mod:`context_schemas`, so the tiers see well-typed methods
    instead of opaque callables.

    The defaults point at the real implementations in
    :mod:`context_readers`, so most call sites just use
    ``RulesReaders()``. A caller that wants a different source swaps
    the one helper it cares about by keyword.
    """

    read_if_exists: ReadIfExists = field(default=read_if_exists)
    read_with_imports: ReadWithImports = field(default=read_with_imports)
    rules_filenames: RulesFilenames = field(default=rules_filenames)
    read_rules_dir: ReadRulesDir = field(default=read_rules_dir)
    read_rules_dir_files: ReadRulesDirFiles = field(default=read_rules_dir_files)


# ── Tier protocol ──────────────────────────────────────────────────


class RulesTier(Protocol):
    """One rules-loading tier the loader knows how to consult.

    Every tier exposes a ``heading`` (the section header it
    contributes when it has content) and a ``load`` method that
    returns the raw rules text. The loader iterates the tiers in
    order and skips the ones that yield an empty string.
    """

    heading: str

    def load(self) -> str: ...


# ── Concrete tiers ─────────────────────────────────────────────────


@dataclass(frozen=True)
class ManagedTier:
    """Sysadmin-enforced managed policy (loaded first).

    The managed directory is a property of the platform, so
    :func:`context_managed.platform_managed_rules_dir` answers it at
    call time — the tier holds no path of its own.
    """

    heading = "Managed Policy"

    readers: RulesReaders
    read_claude_md: bool = True

    def load(self) -> str:
        """The managed-policy instructions file, or ``""`` when there is none."""
        return load_managed_rules(
            self.readers.read_rules_dir,
            self.readers.rules_filenames,
            read_claude_md=self.read_claude_md,
        )


@dataclass(frozen=True)
class MemoryTier:
    """Per-project ``MEMORY.md`` index (loaded after managed, before user)."""

    heading = "Memory Index"

    project_dir: Path
    read_claude_md: bool = True
    paths: MemoryPaths | None = None

    def load(self) -> str:
        """The project's memory index (200-line / 25-KB cap)."""
        return ProjectMemoryBank(
            self.project_dir,
            read_claude_fallback=self.read_claude_md,
            paths=self.paths,
        ).load_index()


@dataclass(frozen=True)
class UserTier:
    """User-level global rules (``~/.igni/rules.md`` / ``~/.igni/rules/`` / ``~/.claude/rules/``)."""

    heading = "User Rules"

    readers: RulesReaders
    paths: UserRulesPaths
    working_dir: Path | None = None
    project_dir: Path | None = None
    read_claude_md: bool = True

    def load(self) -> str:
        """The user's global rules, concatenated over their three locations."""
        return load_user_rules(
            self.readers.read_with_imports,
            self.readers.read_rules_dir_files,
            paths=self.paths,
            working_dir=self.working_dir,
            project_dir=self.project_dir,
            read_claude_rules=self.read_claude_md,
        )


@dataclass(frozen=True)
class ProjectRootTier:
    """Project-root ``igni.md`` / ``CLAUDE.md`` (+ ``.local.md`` overrides)."""

    heading = "Project Rules"

    readers: RulesReaders
    project_dir: Path
    read_claude_md: bool = True

    def load(self) -> str:
        """The project root's rules files, base before local override."""
        return load_project_rules(
            self.readers.read_rules_dir,
            self.readers.rules_filenames,
            project_dir=self.project_dir,
            read_claude_md=self.read_claude_md,
        )


@dataclass(frozen=True)
class ProjectDirsTier:
    """Committed shared rules at ``<project>/.igni/rules/`` and ``<project>/.claude/rules/``."""

    heading = "Project Shared Rules"

    readers: RulesReaders
    project_dir: Path
    working_dir: Path | None = None
    read_claude_md: bool = True

    def load(self) -> str:
        """The committed shared rules of the project's rules directories."""
        return load_project_rules_dirs(
            self.readers.read_rules_dir_files,
            project_dir=self.project_dir,
            working_dir=self.working_dir,
            read_claude_md=self.read_claude_md,
        )


@dataclass(frozen=True)
class SubdirectoryTier:
    """Rules files between the working directory and the project root.

    The one tier that yields several sections — one per walked
    directory — so it does not fit the flat :class:`RulesTier` shape
    the loader's tier loop consumes.
    """

    readers: RulesReaders
    project_dir: Path
    working_dir: Path | None = None
    read_claude_md: bool = True

    def load(self) -> list[SubdirectoryRules]:
        """The walked directories' rules, shallowest first."""
        return load_subdirectory_rules(
            self.readers.read_rules_dir,
            self.readers.rules_filenames,
            project_dir=self.project_dir,
            working_dir=self.working_dir,
            read_claude_md=self.read_claude_md,
        )
