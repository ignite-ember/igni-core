"""The rules tiers the rules-context loader drives.

:mod:`context` owns the coordinator — :class:`RulesContextLoader`, the
class that holds a session's rules state and walks the tiers in order.
This module holds the strategies it drives: the :class:`RulesReaders`
composition (the file-read helpers every tier consumes), the
:class:`RulesTier` protocol, and one class per tier.

## Why this shape

* One class per tier means adding a tier is one class plus one entry
  in the loader's ``TIERS`` tuple — no dispatch dict to keep in sync.
* The tiers hold no state: each one asks the loader for its own
  content, so the session state (project dir, working dir, cross-tool
  toggle) lives in exactly one place.
* The readers arrive as one typed composition, so the tier signatures
  stay free of ``Callable[..., str]`` parameters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

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
)

if TYPE_CHECKING:
    from igni_core.core.utils.context import RulesContextLoader


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


# ── Tier protocol + concrete tiers ─────────────────────────────────


class RulesTier(Protocol):
    """One rules-loading tier the loader knows how to consult.

    Every tier exposes a ``heading`` (the section header it
    contributes when it has content) and a ``load`` method that
    returns the raw rules text. The loader iterates the tiers in
    order and skips the ones that yield an empty string.
    """

    heading: str

    def load(self, loader: RulesContextLoader) -> str: ...


class ManagedTier:
    """Sysadmin-enforced managed policy (loaded first)."""

    heading = "Managed Policy"

    def load(self, loader: RulesContextLoader) -> str:
        return loader.load_managed()


class MemoryTier:
    """Per-project ``MEMORY.md`` index (loaded after managed, before user)."""

    heading = "Memory Index"

    def load(self, loader: RulesContextLoader) -> str:
        return loader.load_memory()


class UserTier:
    """User-level global rules (``~/.igni/rules.md`` / ``~/.igni/rules/`` / ``~/.claude/rules/``)."""

    heading = "User Rules"

    def load(self, loader: RulesContextLoader) -> str:
        return loader.load_user()


class ProjectRootTier:
    """Project-root ``igni.md`` / ``CLAUDE.md`` (+ ``.local.md`` overrides)."""

    heading = "Project Rules"

    def load(self, loader: RulesContextLoader) -> str:
        return loader.load_project_root()


class ProjectDirsTier:
    """Committed shared rules at ``<project>/.igni/rules/`` and ``<project>/.claude/rules/``."""

    heading = "Project Shared Rules"

    def load(self, loader: RulesContextLoader) -> str:
        return loader.load_project_dirs()
