"""Session rules context — loads and merges the six rules tiers.

Rules come from six places, all merged into the session prompt in this
order:

0. **Managed policy** — sysadmin-enforced ``igni.md`` / ``CLAUDE.md``
   in a platform-specific write-protected directory (e.g.
   ``/Library/Application Support/igni/`` on darwin). Prepended first
   so the model sees org-pinned guidance ahead of everything else.
0.5. **Memory index** — the agent's per-project ``MEMORY.md`` from
   ``~/.igni/projects/<slug>/memory/`` (or ``~/.claude/projects/
   <slug>/memory/`` as a cross-tool fallback), capped at 200 lines /
   25 KB.
1. **User-level** — ``~/.igni/rules.md`` (legacy), ``~/.igni/rules/*.md``
   (dir form), plus ``~/.claude/rules/*.md`` when cross-tool support is
   enabled.
2. **Project root** — ``igni.md`` / ``CLAUDE.md`` and their
   ``.local.md`` override siblings.
3. **Project shared rules dirs** — ``<project>/.igni/rules/*.md`` and
   (when cross-tool) ``<project>/.claude/rules/*.md``.
4. **Subdirectory** — ``igni.md`` / ``CLAUDE.md`` in any parent of the
   working file, walking up to the project root.

## The loader

:class:`RulesContextLoader` owns the session-level state — the project
dir, the optional working dir, the cross-tool toggle, the injected
readers and the user's global-rules locations — and drives the tiers
polymorphically. Every tier implements the
:class:`context_loader.RulesTier` protocol, so there is no dispatch
table to keep in sync: adding a tier is one class in
``context_loader``, one loader method here, and one entry in
``TIERS``.

```python
loader = RulesContextLoader(project_dir=Path("."), read_claude_md=True)
instructions = loader.load_all().render()
```

Every collaborator is injected: the file readers arrive as a
:class:`context_loader.RulesReaders` composition, the user's global
rules locations as a :class:`context_schemas.UserRulesPaths` value, and
the managed-policy directory is resolved by
:func:`context_managed.platform_managed_rules_dir` at call time. Nothing
here reads module-level state, so a caller that wants different sources
constructs a different loader rather than patching a global.

The sibling ``context_<tier>.py`` modules (``context_managed`` /
``context_user`` / ``context_project`` / ``context_memory`` /
``context_frontmatter`` / ``context_imports`` / ``context_readers``) hold
the per-tier logic, ``context_loader`` holds the tier strategies, and
``context_schemas`` holds the typed models — one responsibility per file
per CODE_STANDARDS Pattern 8.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from igni_core.core.utils.context_loader import (
    ManagedTier,
    MemoryTier,
    ProjectDirsTier,
    ProjectRootTier,
    RulesReaders,
    RulesTier,
    UserTier,
)
from igni_core.core.utils.context_managed import load_managed_rules as _load_managed_rules
from igni_core.core.utils.context_memory import ProjectMemoryBank
from igni_core.core.utils.context_project import (
    load_project_rules as _load_project_rules,
)
from igni_core.core.utils.context_project import (
    load_project_rules_dirs as _load_project_rules_dirs,
)
from igni_core.core.utils.context_project import (
    load_subdirectory_rules as _load_subdirectory_rules,
)
from igni_core.core.utils.context_schemas import (
    RulesBundle,
    RulesSection,
    SubdirectoryRules,
    UserRulesPaths,
)
from igni_core.core.utils.context_user import default_user_rules_paths
from igni_core.core.utils.context_user import load_user_rules as _load_user_rules

# The module's public surface: the loader plus the typed values it
# takes and returns. The per-tier helpers are imported under private
# aliases so the namespace does not advertise names (``load_project_rules``
# and friends) whose signatures differ from the ones this module used
# to export — a clean removal beats a same-named function with a
# different meaning.
__all__ = [
    "ProjectMemoryBank",
    "RulesBundle",
    "RulesContextLoader",
    "RulesSection",
    "SubdirectoryRules",
    "UserRulesPaths",
]


@dataclass
class RulesContextLoader:
    """Owns the session-level state for a rules load + drives all six tiers.

    Instance state (5 fields, per CODE_STANDARDS checklist):

    * ``project_dir`` — the repo root; anchors every tier.
    * ``working_dir`` — optional; drives the subdirectory walk and
      the ``paths:`` frontmatter filtering.
    * ``read_claude_md`` — cross-tool toggle (every Claude Code
      variant is gated on this).
    * ``readers`` — the composed :class:`context_loader.RulesReaders`
      object holding the injected file-read helpers.
    * ``user_rules_paths`` — the user's global-rules locations, as one
      typed value.

    The managed-policy directory is not stored here: it is a property
    of the platform, so :func:`context_managed.platform_managed_rules_dir`
    answers it at call time.
    """

    project_dir: Path
    working_dir: Path | None = None
    read_claude_md: bool = True
    readers: RulesReaders = field(default_factory=RulesReaders)
    user_rules_paths: UserRulesPaths = field(default_factory=default_user_rules_paths)

    # Ordered tuple of tier objects. The iteration order in
    # ``load_all`` matches the order the session prompt expects:
    # managed → memory → user → project root → project shared →
    # subdirectory chain. Adding a tier is one class in
    # ``context_loader``, one loader method below, and one line here
    # — no dispatch dict to keep in sync.
    TIERS: ClassVar[tuple[RulesTier, ...]] = (
        ManagedTier(),
        MemoryTier(),
        UserTier(),
        ProjectRootTier(),
        ProjectDirsTier(),
    )

    # ── Per-tier loaders ─────────────────────────────────────

    def load_managed(self) -> str:
        """Sysadmin-enforced managed-policy instructions file."""
        return _load_managed_rules(
            self.readers.read_rules_dir,
            self.readers.rules_filenames,
            read_claude_md=self.read_claude_md,
        )

    def load_memory(self) -> str:
        """Per-project ``MEMORY.md`` index (200-line / 25-KB cap)."""
        return ProjectMemoryBank(
            self.project_dir,
            read_claude_fallback=self.read_claude_md,
        ).load_index()

    def load_user(self) -> str:
        """User-level global rules from the injected locations."""
        return _load_user_rules(
            self.readers.read_with_imports,
            self.readers.read_rules_dir_files,
            paths=self.user_rules_paths,
            working_dir=self.working_dir,
            project_dir=self.project_dir,
            read_claude_rules=self.read_claude_md,
        )

    def load_project_root(self) -> str:
        """Project-root ``igni.md`` / ``CLAUDE.md`` (+ ``.local.md`` overrides)."""
        return _load_project_rules(
            self.readers.read_rules_dir,
            self.readers.rules_filenames,
            project_dir=self.project_dir,
            read_claude_md=self.read_claude_md,
        )

    def load_project_dirs(self) -> str:
        """Committed shared rules at ``<project>/.igni/rules/`` + ``<project>/.claude/rules/``."""
        return _load_project_rules_dirs(
            self.readers.read_rules_dir_files,
            project_dir=self.project_dir,
            working_dir=self.working_dir,
            read_claude_md=self.read_claude_md,
        )

    def load_subdirectory(self) -> list[SubdirectoryRules]:
        """Rules files between working-dir and project root, shallowest first."""
        return _load_subdirectory_rules(
            self.readers.read_rules_dir,
            self.readers.rules_filenames,
            project_dir=self.project_dir,
            working_dir=self.working_dir,
            read_claude_md=self.read_claude_md,
        )

    # ── Top-level aggregation ────────────────────────────────

    def load_all(self) -> RulesBundle:
        """Load every tier and assemble the ordered :class:`RulesBundle`.

        Iterates the six tiers polymorphically — no dispatch dict, no
        six-branch if-chain. Subdirectory rules are the one tier that
        can produce multiple sections (one per walked directory),
        handled after the flat-tier loop.
        """
        sections: list[RulesSection] = []
        for tier in self.TIERS:
            body = tier.load(self)
            if body:
                sections.append(RulesSection(heading=tier.heading, body=body))
        for subdir in self.load_subdirectory():
            sections.append(subdir.to_section())
        return RulesBundle(sections=sections)
