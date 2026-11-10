"""What the session is given, and what it starts with.

The settings, the project directory and the collaborators the process
hands over — the supervisor the shell toolkits run on, the prompt
contributions and the prompt loader — arrive with the call that
creates the session; they are not built here. This module puts them
on the session, so that every builder behind them reads them from one
place instead of from a bag that is passed around, and builds the
cloud catalog, which has to exist before anything can read a model.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from igni_core.core.mcp.hints import ToolHintsRegistry
from igni_core.core.prompts.contributions import PromptContributions
from igni_core.core.prompts.fragments import PromptFragments
from igni_core.core.prompts.loader import PromptLoader
from igni_core.core.session.cloud_catalog import CloudModelCatalog
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.workspace import WorkspaceManager

if TYPE_CHECKING:
    from igni_core.core.config.settings import Settings
    from igni_core.core.session.core import Session


@dataclass(frozen=True)
class SessionDeps:
    """What the session was given when it was created.

    Everything here is a choice the caller made. Everything else the
    session holds is built from these. The composer decides which
    builder each one reaches — the project directory and the settings
    are read by most of them, the resumed session id by one.
    """

    settings: Settings
    project_dir: Path | None = None
    resume_session_id: str | None = None
    additional_dirs: list[Path] | None = None
    contributions: PromptContributions | None = None
    prompt_loader: PromptLoader | None = None
    supervisor: ProcessSupervisor | None = None
    tool_hints: ToolHintsRegistry | None = None


class FoundationBuilder:
    """Puts on the session what it was given, and builds the cloud catalog."""

    def __init__(self, session: Session, deps: SessionDeps) -> None:
        self._session = session
        self._deps = deps

    def build(self) -> None:
        """The given values, the place the session runs, and the catalog."""
        session = self._session
        deps = self._deps

        session.settings = deps.settings

        # The supervisor this session's shell toolkits run on. The
        # backend hands the session the supervisor the process shares
        # (one BE = one watcher panel); a session built on its own
        # gets a supervisor of its own.
        session.supervisor = deps.supervisor if deps.supervisor is not None else ProcessSupervisor()

        # What extensions have done to the prompts. Injected: the
        # backend hands the session the process-wide table, so every
        # session of a deployment sees the same contributions; a
        # session built on its own gets a table of its own, empty.
        session.prompt_contributions = (
            deps.contributions if deps.contributions is not None else PromptContributions()
        )

        # igni's prompt files, and the anchors extensions fill into
        # them. Injected for the same reason as the contributions: an
        # anchor an extension supplies has to reach every session of
        # the deployment, and a session built on its own gets a table
        # of its own, empty.
        session.prompt_loader = (
            deps.prompt_loader
            if deps.prompt_loader is not None
            else PromptLoader(PromptFragments())
        )

        # What an extension's tools say about themselves. Injected for
        # the same reason: the installer fills the table, and every
        # session of the deployment must judge those tools by the same
        # hints.
        session.tool_hints = (
            deps.tool_hints if deps.tool_hints is not None else ToolHintsRegistry.shared()
        )

        # Every code path below may read ``settings.models.default``, so
        # the cloud catalog is refreshed first: a brand-new install reaches
        # a usable state right after login without a hardcoded fallback.
        session.cloud_catalog = CloudModelCatalog(deps.settings)
        session.cloud_catalog.refresh()

        # Where the session runs, and what it may reach.
        session.project_dir = deps.project_dir or Path.cwd()
        session.workspace = WorkspaceManager(session.project_dir, deps.additional_dirs)


__all__ = ["FoundationBuilder", "SessionDeps"]
