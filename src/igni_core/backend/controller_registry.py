"""Builds every ``BackendServer`` sub-controller, once, eagerly.

* :class:`Controllers` — a plain attribute-holder class with one
  slot per controller (mcp / hitl / context / auth / …).
* :class:`ControllerRegistry` — the builder. Wires each controller
  to its collaborators (other controllers, stores, the message
  queue) directly; no controller holds a reference back to
  ``BackendServer``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from igni_core.backend.group_policy_sync import GroupPolicySync
from igni_core.backend.hitl_controller import HitlController
from igni_core.backend.hitl_tracer import HITLTracer
from igni_core.backend.marketplace_controller import MarketplaceController
from igni_core.backend.model_switcher import ModelSwitcher
from igni_core.backend.pending_requirements_store import PendingRequirementsStore
from igni_core.backend.plan_snapshot_builder import PlanSnapshotBuilder
from igni_core.backend.plugin_controller import PluginController
from igni_core.backend.run_controller import RunController
from igni_core.backend.server_auth import AuthController
from igni_core.backend.server_context import ContextController
from igni_core.backend.server_files import FilesController
from igni_core.backend.server_history import ChatHistoryRebuilder
from igni_core.backend.server_lifecycle import LifecycleController
from igni_core.backend.server_loop import LoopController
from igni_core.backend.server_mcp import McpController
from igni_core.backend.server_panels import PanelsController
from igni_core.backend.server_processes import ProcessesController
from igni_core.backend.server_rehydrate import RehydrateController
from igni_core.backend.server_search import SearchController
from igni_core.backend.server_sessions import SessionsController
from igni_core.backend.team_wiring import TeamWiring
from igni_core.backend.visualization_action_bus import VisualizationActionBus
from igni_core.core.auth.discovery import ServerDiscovery
from igni_core.core.auth.portal_client import PortalClient
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.config.group_policy.meta import PackMetaStore
from igni_core.core.config.models import ModelRegistry
from igni_core.core.tools.process_supervisor import ProcessSupervisor

if TYPE_CHECKING:
    from igni_core.core.config.settings import Settings
    from igni_core.core.session import Session
    from igni_core.core.session.pending_messages import PendingMessageStore
    from igni_core.core.session.queued_messages import DurableMessageQueue
    from igni_core.core.session.session_preferences import SessionPreferencesStore


class Controllers:
    """Typed attribute bag — one slot per sub-controller.

    Kept as a plain class (not a Pydantic model) because most of
    the values are opaque controller objects with their own lazy
    subsystems that would fight Pydantic's field validation.

    Consumers (``BackendServer`` facades) access members by name:
    ``self.controllers.mcp``, ``self.controllers.hitl``, etc.
    """

    def __init__(
        self,
        *,
        mcp: McpController,
        hitl: HitlController,
        context: ContextController,
        auth: AuthController,
        group_policy: GroupPolicySync,
        files: FilesController,
        search: SearchController,
        panels: PanelsController,
        loop: LoopController,
        processes: ProcessesController,
        sessions: SessionsController,
        rehydrate: RehydrateController,
        lifecycle: LifecycleController,
        plan_snapshots: PlanSnapshotBuilder,
        viz_actions: VisualizationActionBus,
        team_wiring: TeamWiring,
        model_switcher: ModelSwitcher,
        plugins: PluginController,
        marketplaces: MarketplaceController,
        runs: RunController,
        chat_history: ChatHistoryRebuilder,
    ) -> None:
        self.mcp = mcp
        self.hitl = hitl
        self.context = context
        self.auth = auth
        self.group_policy = group_policy
        self.files = files
        self.search = search
        self.panels = panels
        self.loop = loop
        self.processes = processes
        self.sessions = sessions
        self.rehydrate = rehydrate
        self.lifecycle = lifecycle
        self.plan_snapshots = plan_snapshots
        self.viz_actions = viz_actions
        self.team_wiring = team_wiring
        self.model_switcher = model_switcher
        self.plugins = plugins
        self.marketplaces = marketplaces
        self.runs = runs
        self.chat_history = chat_history


class ControllerRegistry:
    """Builds the :class:`Controllers` bag for a ``BackendServer``."""

    @classmethod
    def build(
        cls,
        *,
        session: Session,
        settings: Settings,
        hitl_store: PendingRequirementsStore,
        pending_store: PendingMessageStore,
        session_prefs: SessionPreferencesStore,
        user_config_store: Any,
        hitl_tracer: HITLTracer | None,
        message_queue: DurableMessageQueue | None,
        supervisor: ProcessSupervisor,
        gate: RunGate,
    ) -> Controllers:
        """Full production wiring — every controller built eagerly.

        ``supervisor`` is the supervisor the backend process shares —
        the registry the watcher panel reads and the foreground slot
        the cancel path kills. It is injected rather than looked up,
        so every controller of one backend process talks to the same
        one. ``gate`` is injected for the same reason: it is the one
        gate of the process, and every controller that starts an agent
        run asks it before it starts one.
        """
        # ── Foundation controllers (session-only deps) ──────────────
        mcp = McpController(session)
        files = FilesController(session)
        search = SearchController(session)
        panels = PanelsController(session)
        processes = ProcessesController(supervisor)
        rehydrate = RehydrateController(session, supervisor)
        plan_snapshots = PlanSnapshotBuilder(session)
        viz_actions = VisualizationActionBus(session)
        team_wiring = TeamWiring(session)
        plugins = PluginController(session)
        marketplaces = MarketplaceController(session)

        # HITL — the controller now owns the pause_handler + tracer.
        hitl = HitlController(
            session=session,
            store=hitl_store,
            tracer=hitl_tracer,
        )

        # RunController owns the run pipeline (lock, task, checkpoint).
        runs = RunController(
            session=session,
            pending_store=pending_store,
            message_queue=message_queue,
            stream_factory=hitl.stream_with_subagent,
            supervisor=supervisor,
            gate=gate,
        )

        # Context needs pending_store for the crash-survival RPC.
        context = ContextController(
            session=session,
            settings=settings,
            pending_store=pending_store,
            models=ModelRegistry(settings),
            pack_meta=PackMetaStore(GroupPolicyLayout.for_data_dir(settings.storage.data_dir)),
        )

        chat_history = ChatHistoryRebuilder(session=session)
        sessions = SessionsController(
            session=session,
            chat_history_provider=chat_history.rebuild,
        )

        # One portal client, shared: sign-in and the group-policy fetch
        # must talk to the same server. ``portal_url`` is passed through
        # rather than left to the derivation, which only rewrites host
        # labels and so cannot express a portal that differs from the API
        # by port; empty keeps the derivation.
        portal = PortalClient(api_url=settings.api_url, portal_url=settings.portal_url or None)
        group_policy = GroupPolicySync(session=session, settings=settings, portal=portal)
        auth = AuthController(
            session=session,
            settings=settings,
            status_provider=context.get_status,
            portal=portal,
            group_policy=group_policy,
            user_config_store=user_config_store,
            server_discovery=ServerDiscovery(),
        )
        # Cold start: refresh from a stored token and start the timer.
        group_policy.start()

        # Loop reads settings for the pump.
        loop = LoopController(
            session=session,
            settings=settings,
            as_run=runs.run_unstreamed,
            gate=gate,
        )

        lifecycle = LifecycleController(
            session=session,
            pending_store=pending_store,
            runs=runs,
            rehydrate=rehydrate,
            scheduler_stop=loop.scheduler.stop,
            message_queue=message_queue,
        )

        model_switcher = ModelSwitcher(
            session=session,
            session_prefs=session_prefs,
            user_config_store=user_config_store,
        )

        return Controllers(
            mcp=mcp,
            hitl=hitl,
            context=context,
            auth=auth,
            group_policy=group_policy,
            files=files,
            search=search,
            panels=panels,
            loop=loop,
            processes=processes,
            sessions=sessions,
            rehydrate=rehydrate,
            lifecycle=lifecycle,
            plan_snapshots=plan_snapshots,
            viz_actions=viz_actions,
            team_wiring=team_wiring,
            model_switcher=model_switcher,
            plugins=plugins,
            marketplaces=marketplaces,
            runs=runs,
            chat_history=chat_history,
        )
