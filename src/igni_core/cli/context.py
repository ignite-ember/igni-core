"""Typed :class:`click.Context.obj` payload."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from igni_core.core.config.settings import Settings
from igni_core.core.worktree import WorktreeManager


class CliContext(BaseModel):
    """Everything the CLI stashes on :attr:`click.Context.obj`.

    Subcommands read it by attribute (``ctx.obj.settings``), so a
    shape drift surfaces as an ``AttributeError`` rather than a
    silent missing key.
    """

    # WorktreeManager is a plain class, so it is checked by isinstance.
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    settings: Settings
    worktree_manager: WorktreeManager | None = None
    project_dir: Path | None = None
    additional_dirs: list[Path] | None = None
