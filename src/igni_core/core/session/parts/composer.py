"""The order a session is built in.

:class:`~igni_core.core.session.core.Session` is the aggregate that
holds the sub-systems; this package builds them. Each module owns one
concern and exposes one builder class; :class:`SessionComposer` is the
order those builders are called in.

The order is not incidental. The hooks are loaded before the MCP
manager exists, the identity before the persister that keys its rows,
the pools and the learning manager before the main team that draws on
them. Where a sub-system needs one that is not built yet, it is given
a callable to read rather than a value, so the late binding is visible
in the code instead of implied by the order.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from igni_core.core.prompts.contributions import PromptContributions
from igni_core.core.prompts.fragments import PromptFragments
from igni_core.core.prompts.loader import PromptLoader
from igni_core.core.session.cloud_catalog import CloudModelCatalog
from igni_core.core.session.compaction import ContextBudget
from igni_core.core.session.parts.agents import AgentsBuilder
from igni_core.core.session.parts.coordinators import CoordinatorBuilder
from igni_core.core.session.parts.mcp import McpBuilder
from igni_core.core.session.parts.persistence import PersistenceBuilder
from igni_core.core.session.parts.plugins import PluginBuilder
from igni_core.core.session.parts.project import ProjectBuilder
from igni_core.core.session.parts.runtime import RuntimeBuilder
from igni_core.core.session.parts.stores import StoresBuilder
from igni_core.core.session.parts.tooling import ToolingBuilder
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.workspace import WorkspaceManager

if TYPE_CHECKING:
    from igni_core.core.config.settings import Settings
    from igni_core.core.session.core import Session


@dataclass(frozen=True)
class SessionComposerDeps:
    """What the session was given when it was created.

    Everything here is a choice the caller made. Everything else the
    session holds is built from these.
    """

    settings: Settings
    project_dir: Path | None = None
    resume_session_id: str | None = None
    additional_dirs: list[Path] | None = None
    contributions: PromptContributions | None = None
    prompt_loader: PromptLoader | None = None
    supervisor: ProcessSupervisor | None = None


class SessionComposer:
    """Builds every sub-system the session holds, in dependency order.

    One session, one composer: the builders it calls fill the session's
    declared surface, and the order they are called in is the only
    thing that decides which of them may read which.
    """

    def __init__(self, session: Session, deps: SessionComposerDeps) -> None:
        self._session = session
        self._deps = deps

    def compose(self) -> None:
        """Fill the session, one concern at a time."""
        session = self._session
        deps = self._deps

        # ── What the session was given ──────────────────────────
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

        # Every code path below may read ``settings.models.default``, so
        # the cloud catalog is refreshed first: a brand-new install reaches
        # a usable state right after login without a hardcoded fallback.
        session.cloud_catalog = CloudModelCatalog(deps.settings)
        session.cloud_catalog.refresh()

        # ── Where the session runs ───────────────────────────────
        session.project_dir = deps.project_dir or Path.cwd()
        session.workspace = WorkspaceManager(session.project_dir, deps.additional_dirs)

        # ── The scratch the tools write into ─────────────────────
        stores = StoresBuilder(session)
        stores.build_loop_state()
        stores.build_scratch()

        # ── The project's own context ────────────────────────────
        project = ProjectBuilder(session)
        project.build_group_policy()
        project.run_initializer()

        # ── The database, and the session's outward faces ────────
        PersistenceBuilder(session).build_database()
        RuntimeBuilder(session).build_runtime_services()

        # ── Plugins, hooks and the pools they feed ───────────────
        plugins = PluginBuilder(session)
        plugins.build_hooks()
        project.build_context()
        plugins.build_plugins_and_styles()
        AgentsBuilder(session).build_pools()

        session.context_budget = ContextBudget.for_settings(deps.settings)

        # ── Who the session is ───────────────────────────────────
        runtime = RuntimeBuilder(session)
        runtime.build_cloud_auth()
        runtime.build_identity(deps.resume_session_id)

        # ── The servers, the tooling, the memory ─────────────────
        McpBuilder(session).build_mcp()
        ToolingBuilder(session).build()
        PersistenceBuilder(session).build_persistence()

        # ── The collaborators that own the behaviour ─────────────
        CoordinatorBuilder(session).build()
        McpBuilder(session).build_lifecycle()
        plugins.build_reload_orchestrator()

        # ── The agent the session talks to ───────────────────────
        AgentsBuilder(session).build_main_team()
        runtime.build_message_handler()


__all__ = ["SessionComposer", "SessionComposerDeps"]
