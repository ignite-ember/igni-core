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

from typing import TYPE_CHECKING

from igni_core.core.session.compaction import ContextBudget
from igni_core.core.session.parts.agents import AgentsBuilder
from igni_core.core.session.parts.coordinators import CoordinatorBuilder
from igni_core.core.session.parts.foundation import FoundationBuilder, SessionDeps
from igni_core.core.session.parts.mcp import McpBuilder
from igni_core.core.session.parts.persistence import PersistenceBuilder
from igni_core.core.session.parts.plugins import PluginBuilder
from igni_core.core.session.parts.project import ProjectBuilder
from igni_core.core.session.parts.runtime import RuntimeBuilder
from igni_core.core.session.parts.stores import StoresBuilder
from igni_core.core.session.parts.tooling import ToolingBuilder

if TYPE_CHECKING:
    from igni_core.core.session.core import Session


class SessionComposer:
    """Builds every sub-system the session holds, in dependency order.

    One session, one composer: the builders it calls fill the session's
    declared surface, and the order they are called in is the only
    thing that decides which of them may read which.
    """

    def __init__(self, session: Session, deps: SessionDeps) -> None:
        self._session = session
        self._deps = deps

    def compose(self) -> None:
        """Fill the session, one concern at a time."""
        session = self._session
        deps = self._deps

        # ── What the session was given ──────────────────────────
        FoundationBuilder(session, deps).build()

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


__all__ = ["SessionComposer", "SessionDeps"]
