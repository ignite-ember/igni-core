"""User-level global rules — ``~/.igni/rules.md`` legacy file +
``~/.igni/rules/*.md`` directory form + ``~/.claude/rules/*.md``
cross-tool form.

Extracted from :mod:`igni_core.core.utils.context` per
CODE_STANDARDS.md Pattern 8. Same dependency-injection design as
:mod:`context_managed`: this module holds the paths + the loader
function; the shared rules-reading helpers (``_read_with_imports``,
``_read_rules_dir_files``) are passed in by the caller so this
module stays a leaf in the import graph and CODE_STANDARDS Rule 2
(no inline imports) holds.

## Sources loaded (in order)

1. ``~/.igni/rules.md`` — legacy single-file form. Kept for users
   who set that up before the directory form was added.
2. ``~/.igni/rules/*.md`` — igni-native directory form. One file
   per topic (e.g. ``coding.md``, ``testing.md``). Files with a
   ``paths:`` frontmatter contribute only when the session's
   ``working_dir`` matches one of the globs.
3. ``~/.claude/rules/*.md`` — cross-tool form, gated on
   ``read_claude_rules``. Lets a user who has Claude Code rules
   set up already have them apply to igni sessions too without
   duplicating.
"""

from __future__ import annotations

from pathlib import Path

from igni_core.core.paths import CONFIG_DIR
from igni_core.core.utils.context_schemas import (
    ReadRulesDirFiles,
    ReadWithImports,
    UserRulesPaths,
)

# Path constants — the user's real global-rules locations. The loader
# reads them through :func:`default_user_rules_paths`, so a caller that
# wants different locations injects a :class:`UserRulesPaths` value
# instead of relying on these.
USER_RULES_PATH = Path.home() / CONFIG_DIR / "rules.md"
USER_RULES_DIR = Path.home() / CONFIG_DIR / "rules"
CLAUDE_USER_RULES_DIR = Path.home() / ".claude" / "rules"


def default_user_rules_paths() -> UserRulesPaths:
    """The user's real global-rules locations.

    Built from the module-level constants, which are resolved from
    ``Path.home()`` when this module is imported. A loader takes a
    snapshot of them at construction time, so a caller that wants
    different locations injects its own :class:`UserRulesPaths`.
    """
    return UserRulesPaths(
        legacy_file=USER_RULES_PATH,
        rules_dir=USER_RULES_DIR,
        claude_rules_dir=CLAUDE_USER_RULES_DIR,
    )


def load_user_rules(
    read_with_imports: ReadWithImports,
    read_rules_dir_files: ReadRulesDirFiles,
    paths: UserRulesPaths,
    working_dir: Path | None = None,
    project_dir: Path | None = None,
    read_claude_rules: bool = True,
) -> str:
    """Load user-level global rules from all configured sources.

    Sources, concatenated in order:

    1. ``~/.igni/rules.md`` (legacy single-file form)
    2. ``~/.igni/rules/*.md`` (directory form)
    3. ``~/.claude/rules/*.md`` (cross-tool, when ``read_claude_rules``)

    Files with ``paths:`` frontmatter contribute only when
    ``working_dir`` matches one of the globs.

    ``paths`` says where the three sources live; the caller injects
    it. ``read_with_imports`` and ``read_rules_dir_files`` are
    injected too — they are shared across all rule sources, and
    passing them by argument keeps this module a leaf in the import
    graph (the loader imports us; we don't import back).
    """
    sections: list[str] = []
    legacy = read_with_imports(paths.legacy_file, allowed_root=paths.legacy_file.parent)
    if legacy:
        sections.append(legacy)
    igni_dir = read_rules_dir_files(paths.rules_dir, working_dir, project_dir)
    if igni_dir:
        sections.append(igni_dir)
    if read_claude_rules:
        claude_dir = read_rules_dir_files(paths.claude_rules_dir, working_dir, project_dir)
        if claude_dir:
            sections.append(claude_dir)
    return "\n\n".join(sections)
