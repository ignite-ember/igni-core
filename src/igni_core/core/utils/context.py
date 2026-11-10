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
``context_loader`` and one entry in the loader's ``tiers()``.

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
from typing import TypeVar

from igni_core.core.utils.context_loader import (
    ManagedTier,
    MemoryTier,
    ProjectDirsTier,
    ProjectRootTier,
    RulesReaders,
    RulesTier,
    SubdirectoryTier,
    UserTier,
)
from igni_core.core.utils.context_memory_paths import MemoryPaths
from igni_core.core.utils.context_schemas import (
    RulesBundle,
    RulesSection,
    SubdirectoryRules,
    UserRulesPaths,
)
from igni_core.core.utils.context_user import default_user_rules_paths

# The module's public surface: the loader plus the typed values it
# takes and returns.
__all__ = [
    "RulesBundle",
    "RulesContextLoader",
    "RulesSection",
    "SubdirectoryRules",
    "UserRulesPaths",
]

TierT = TypeVar("TierT", bound=RulesTier)


@dataclass
class RulesContextLoader:
    """Owns the session-level state for a rules load + drives all six tiers.

    Instance state (6 fields, per CODE_STANDARDS checklist):

    * ``project_dir`` — the repo root; anchors every tier.
    * ``working_dir`` — optional; drives the subdirectory walk and
      the ``paths:`` frontmatter filtering.
    * ``read_claude_md`` — cross-tool toggle (every Claude Code
      variant is gated on this).
    * ``readers`` — the composed :class:`context_loader.RulesReaders`
      object holding the injected file-read helpers.
    * ``user_rules_paths`` — the user's global-rules locations, as one
      typed value.
    * ``memory_paths`` — where the project's memory files live, as one
      typed value; ``None`` means the default layout.

    The managed-policy directory is not stored here: it is a property
    of the platform, so :func:`context_managed.platform_managed_rules_dir`
    answers it at call time.
    """

    project_dir: Path
    working_dir: Path | None = None
    read_claude_md: bool = True
    readers: RulesReaders = field(default_factory=RulesReaders)
    user_rules_paths: UserRulesPaths = field(default_factory=default_user_rules_paths)
    memory_paths: MemoryPaths | None = None

    # ── Tier composition ─────────────────────────────────────

    def tiers(self) -> tuple[RulesTier, ...]:
        """The flat tiers, in the order the session prompt expects.

        managed → memory → user → project root → project shared.
        Every tier is built here, from this loader's state, so there
        is exactly one construction site: adding a tier is one class
        in ``context_loader`` and one entry in this tuple — no
        dispatch dict to keep in sync.

        The subdirectory chain is not one of them: it yields one
        section per walked directory, so it is loaded separately by
        :meth:`load_subdirectory`.
        """
        return (
            ManagedTier(
                readers=self.readers,
                read_claude_md=self.read_claude_md,
            ),
            MemoryTier(
                project_dir=self.project_dir,
                read_claude_md=self.read_claude_md,
                paths=self.memory_paths,
            ),
            UserTier(
                readers=self.readers,
                paths=self.user_rules_paths,
                working_dir=self.working_dir,
                project_dir=self.project_dir,
                read_claude_md=self.read_claude_md,
            ),
            ProjectRootTier(
                readers=self.readers,
                project_dir=self.project_dir,
                read_claude_md=self.read_claude_md,
            ),
            ProjectDirsTier(
                readers=self.readers,
                project_dir=self.project_dir,
                working_dir=self.working_dir,
                read_claude_md=self.read_claude_md,
            ),
        )

    def _tier(self, tier_type: type[TierT]) -> TierT:
        """The loader's tier of the given type, built from its state."""
        for tier in self.tiers():
            if isinstance(tier, tier_type):
                return tier
        raise LookupError(f"this loader has no {tier_type.__name__} tier")

    # ── Per-tier loaders ─────────────────────────────────────

    def load_managed(self) -> str:
        """Sysadmin-enforced managed-policy instructions file."""
        return self._tier(ManagedTier).load()

    def load_memory(self) -> str:
        """Per-project ``MEMORY.md`` index (200-line / 25-KB cap)."""
        return self._tier(MemoryTier).load()

    def load_user(self) -> str:
        """User-level global rules from the injected locations."""
        return self._tier(UserTier).load()

    def load_project_root(self) -> str:
        """Project-root ``igni.md`` / ``CLAUDE.md`` (+ ``.local.md`` overrides)."""
        return self._tier(ProjectRootTier).load()

    def load_project_dirs(self) -> str:
        """Committed shared rules at ``<project>/.igni/rules/`` + ``<project>/.claude/rules/``."""
        return self._tier(ProjectDirsTier).load()

    def load_subdirectory(self) -> list[SubdirectoryRules]:
        """Rules files between working-dir and project root, shallowest first."""
        return SubdirectoryTier(
            readers=self.readers,
            project_dir=self.project_dir,
            working_dir=self.working_dir,
            read_claude_md=self.read_claude_md,
        ).load()

    # ── Top-level aggregation ────────────────────────────────

    def load_all(self) -> RulesBundle:
        """Load every tier and assemble the ordered :class:`RulesBundle`.

        Iterates the tiers polymorphically — no dispatch dict, no
        six-branch if-chain. Subdirectory rules are the one tier that
        can produce multiple sections (one per walked directory),
        handled after the flat-tier loop.
        """
        sections: list[RulesSection] = []
        for tier in self.tiers():
            body = tier.load()
            if body:
                sections.append(RulesSection(heading=tier.heading, body=body))
        for subdir in self.load_subdirectory():
            sections.append(subdir.to_section())
        return RulesBundle(sections=sections)
