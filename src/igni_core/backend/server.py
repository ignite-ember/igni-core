"""Backend server — the composition root and typed facade of one session.

:class:`BackendServer` builds the session's collaborators through
:class:`BackendBootstrap`, builds every sub-controller once through
:class:`ControllerRegistry`, and exposes the :class:`Controllers` bag
as :attr:`BackendServer.controllers`.

The methods on the class are the duck-typed surface the message
dispatcher, push bridge, session pool and host services call (and the
test fakes implement). Each is a one-line delegate; logic lives on the
controllers. Callers with a single use reach the controller directly
(``backend.controllers.<name>.<method>()``).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, MutableSequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from igni_core.backend.activity import SessionActivity
from igni_core.backend.backend_bootstrap import BackendBootstrap
from igni_core.backend.command_handler import CommandHandler
from igni_core.backend.controller_registry import ControllerRegistry, Controllers
from igni_core.backend.schemas_sessions import AutoNameResult
from igni_core.backend.workspace_trust_controller import WorkspaceTrustController, reload_settings
from igni_core.core.broadcast_schema import BroadcastSink
from igni_core.core.prompts.contributions import PromptContributions
from igni_core.core.prompts.loader import PromptLoader
from igni_core.core.session.event_log_schema import (
    SessionEvent,
    SessionEventAppendResult,
    SessionEventDraft,
    SessionEventKind,
)
from igni_core.core.session.schemas import PersistResult
from igni_core.protocol import messages as msg

if TYPE_CHECKING:
    from igni_core.backend.schemas_context import (
        InterruptedRun,
        PendingMessage,
        TruncateHistoryResult,
    )
    from igni_core.backend.schemas_history import ChatSearchHit, ChatTurn
    from igni_core.backend.schemas_loop import LoopStatusSnapshot
    from igni_core.backend.schemas_mcp import MCPServerSnapshot, MCPServerSummary
    from igni_core.backend.schemas_panels import (
        HookEntryView,
        OutputStylesResult,
        PromoteEphemeralResult,
        SlashCommandEntry,
    )
    from igni_core.backend.schemas_plan import LatestPlanResult
    from igni_core.backend.schemas_rpc import GroupPolicyPackResult
    from igni_core.backend.schemas_visualization import (
        VisualizationAction,
        VisualizationActionResult,
    )
    from igni_core.backend.server_files import ReadFileResult
    from igni_core.backend.server_processes import ProcessRow
    from igni_core.core.agents import AgentInfo
    from igni_core.core.config.settings import Settings
    from igni_core.core.plugins.models import MarketplaceInfo, PluginInfo
    from igni_core.core.scheduler.models import ScheduledTask
    from igni_core.core.session.core import Session
    from igni_core.core.session.queued_messages import DurableMessageQueue
    from igni_core.core.session.schemas import ContextBreakdown, McpServerStatus
    from igni_core.core.skills import SkillPool
    from igni_core.core.skills.parser import SkillInfo
    from igni_core.core.tools.orchestrate_events import OnProgress
    from igni_core.core.tools.todo import TodoItemWire
    from igni_core.core.utils.media_schemas import MediaAttachments

logger = logging.getLogger(__name__)


class BackendServer:
    """One session's composition root: the session, its settings, the
    controllers built over them, and the durable message queue."""

    def __init__(
        self,
        settings: Settings,
        project_dir: Path | None = None,
        resume_session_id: str | None = None,
        additional_dirs: list[Path] | None = None,
        contributions: PromptContributions | None = None,
        prompt_loader: PromptLoader | None = None,
    ):
        bootstrap = BackendBootstrap(
            settings=settings,
            project_dir=project_dir,
            resume_session_id=resume_session_id,
            additional_dirs=additional_dirs,
            contributions=contributions,
            prompt_loader=prompt_loader,
        )
        controllers = ControllerRegistry.build(
            session=bootstrap.session,
            settings=bootstrap.settings,
            hitl_store=bootstrap.hitl_store,
            pending_store=bootstrap.pending_store,
            session_prefs=bootstrap.session_prefs,
            user_config_store=bootstrap.user_config_store,
            hitl_tracer=bootstrap.hitl_tracer,
            message_queue=bootstrap.message_queue,
        )
        self._adopt(bootstrap.session, bootstrap.settings, controllers, bootstrap.message_queue)

    @classmethod
    def from_parts(
        cls,
        *,
        session: Session,
        settings: Settings,
        controllers: Controllers,
        message_queue: DurableMessageQueue | None,
    ) -> BackendServer:
        """Assemble a server from collaborators that are already built.

        The only alternate constructor: it skips :class:`BackendBootstrap`
        (tests supply their own session and stores) and goes through the
        same :meth:`_adopt` as :meth:`__init__`.
        """
        server = cls.__new__(cls)
        server._adopt(session, settings, controllers, message_queue)
        return server

    def _adopt(
        self,
        session: Session,
        settings: Settings,
        controllers: Controllers,
        message_queue: DurableMessageQueue | None,
    ) -> None:
        self._session = session
        self._settings = settings
        self.controllers = controllers
        #: Messages typed during a run, durable — the runtime's queue.
        self.message_queue = message_queue
        self._runtime: Any = None
        # A new dialogue should pick up an admin's change.
        session.on_new_dialogue = controllers.group_policy.revalidate_for_new_dialogue

    # ── Runtime + session identity ────────────────────────────────

    def attach_runtime(self, runtime: Any) -> None:
        """Called by :class:`SessionRuntimeFactory` once the runtime
        that owns this backend is built."""
        self._runtime = runtime

    @property
    def runtime(self) -> Any:
        return self._runtime

    @property
    def project_dir(self) -> Path:
        return self._session.project_dir

    @property
    def mcp_manager(self) -> Any:
        """The session's MCP client manager, for the extension installer
        and health monitor."""
        return self._session.mcp_manager

    @property
    def session_id(self) -> str:
        return self._session.session_id

    @property
    def session(self) -> Session:
        return self._session

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def run_timeout(self) -> int:
        return self._settings.models.max_run_timeout

    @property
    def processing(self) -> bool:
        return self.controllers.runs.is_processing()

    def activity(self) -> SessionActivity:
        """Running, waiting on a person, or idle (``backend/activity.py``)."""
        waiting = sum(len(m.requirements) for m in self.controllers.hitl.waiting_messages())
        return SessionActivity.of(
            self.session_id,
            run_in_progress=self.processing,
            waiting_approvals=waiting,
            blocked_spawns=self._session.sub_agent_hitl.waiting_count,
            active_subagent_runs=self.controllers.team_wiring.active_subagent_runs(),
        )

    # ── Lifecycle ─────────────────────────────────────────────────

    async def startup(self) -> None:
        await self.controllers.lifecycle.startup()

    async def shutdown(self) -> None:
        await self.controllers.lifecycle.shutdown()

    def start_boot_background_services(self) -> None:
        self._session.start_marketplace_refresh_background()

    def register_broadcast(self, subscriber: BroadcastSink) -> None:
        self._session.broadcast_bus.register(subscriber)

    def reload_agents(self) -> bool:
        """Rebuild this session's agents; ``False`` keeps the working ones."""
        return self._session.reload_group_agents()

    async def fire_session_start_hook(self) -> None:
        """Revalidate the group pack, then fire the session's start hook.

        Before the hook so the hook and the first turn see the same
        configuration. A failed revalidation leaves the cached pack in
        place: a session that will not start is worse than one running a
        slightly stale pack.
        """
        try:
            await self.controllers.group_policy.revalidate_for_new_dialogue()
        except Exception:
            logger.warning(
                "group-policy revalidation failed; continuing with cached pack", exc_info=True
            )
        await self._session.fire_session_start_hook()

    # ── Runs ──────────────────────────────────────────────────────

    async def run_message(
        self, text: str, media: MediaAttachments | None = None
    ) -> AsyncIterator[msg.Message]:
        async for proto in self.controllers.runs.run_message(text, media):
            yield proto

    def cancel_run(self) -> None:
        self.controllers.runs.cancel_run()

    def consume_plan_research_flag(self) -> bool:
        return self._session.plan.consume_research_flag()

    async def resolve_hitl_batch(
        self, decisions: list[msg.HITLDecision]
    ) -> AsyncIterator[msg.Message]:
        async for proto in self.controllers.hitl.resolve_batch(decisions):
            yield proto

    async def resolve_hitl(
        self, requirement_id: str, action: str, choice: str = "once"
    ) -> AsyncIterator[msg.Message]:
        async for proto in self.controllers.hitl.resolve_single(requirement_id, action, choice):
            yield proto

    async def handle_command(self, text: str) -> msg.CommandResult:
        return await CommandHandler(self._session).handle(text)

    def wire_queue_hook(self, queue: MutableSequence[str]) -> None:
        self.controllers.team_wiring.wire_queue_hook(queue)

    def wire_orchestrate_progress(self, callback: OnProgress) -> None:
        self.controllers.team_wiring.wire_orchestrate_progress(callback)

    # ── Sessions + history ────────────────────────────────────────

    async def list_sessions(self) -> msg.SessionListResult:
        return await self.controllers.sessions.list_sessions()

    async def maybe_auto_name_session(self) -> AutoNameResult:
        """A run in progress has the model: the session is titled
        without it rather than queueing a call behind the person's work."""
        return await self.controllers.sessions.maybe_auto_name_session(model_busy=self.processing)

    async def title_session(self, name: str) -> PersistResult:
        return await self.controllers.sessions.title(name)

    async def switch_session(self, session_id: str) -> msg.Info:
        return await self.controllers.sessions.switch_session(session_id)

    async def search_chat(
        self, session_id: str, query: str, limit: int = 50
    ) -> list[ChatSearchHit]:
        return await self.controllers.sessions.search_chat(session_id, query, limit)

    async def get_chat_history(self, session_id: str) -> list[ChatTurn]:
        return await self.controllers.chat_history.rebuild(session_id)

    async def record_event(self, event: SessionEventDraft) -> SessionEventAppendResult:
        """Keep an event in the session's log, which a reload replays.

        Returns what the append did — a failed write is reported,
        not swallowed.
        """
        return await self._session.append_event(event)

    def session_events(self, event_type: SessionEventKind) -> list[SessionEvent]:
        """The session's logged events of one kind, oldest first."""
        return [e for e in self._session.event_log if e.type == event_type]

    # ── Context ───────────────────────────────────────────────────

    def get_status(self) -> msg.StatusUpdate:
        return self.controllers.context.get_status()

    async def count_context_tokens(self) -> int:
        return await self.controllers.context.count_context_tokens()

    async def context_breakdown(self) -> ContextBreakdown:
        return await self.controllers.context.context_breakdown()

    async def get_pending_messages(self, session_id: str) -> list[PendingMessage]:
        return await self.controllers.context.get_pending_messages(session_id)

    async def get_interrupted_runs(self, session_id: str) -> list[InterruptedRun]:
        return await self.controllers.context.get_interrupted_runs(session_id)

    async def truncate_history(self, session_id: str, run_id: str) -> TruncateHistoryResult:
        return await self.controllers.context.truncate_history(session_id, run_id)

    def switch_model(self, model_name: str) -> msg.Info | msg.Error:
        return self.controllers.model_switcher.switch(model_name)

    def toggle_verbose(self) -> bool:
        return self._settings.display.toggle_show_routing()

    # ── MCP ───────────────────────────────────────────────────────

    async def toggle_mcp(self, server_name: str, connect: bool) -> msg.Info:
        return await self.controllers.mcp.toggle(server_name, connect)

    def get_mcp_status(self) -> list[McpServerStatus]:
        return self.controllers.mcp.status()

    async def get_mcp_server_details(self) -> list[MCPServerSnapshot]:
        return await self.controllers.mcp.server_details()

    def get_mcp_servers(self) -> list[MCPServerSummary]:
        return self.controllers.mcp.servers()

    async def mcp_connect(self, server_name: str) -> msg.Info:
        return await self.controllers.mcp.connect(server_name)

    # ── /loop + scheduler ─────────────────────────────────────────

    async def loop_pause(self) -> bool:
        return await self.controllers.loop.pause()

    async def loop_status(self) -> LoopStatusSnapshot:
        return await self.controllers.loop.status()

    async def get_scheduled_tasks(self, include_done: bool = True) -> list[ScheduledTask]:
        return await self.controllers.loop.scheduler.list_all(include_done)

    # ── Files, plan, todos, visualization, processes ──────────────

    def read_file(self, path: str) -> ReadFileResult:
        return self.controllers.files.read_file(path)

    def get_latest_plan(self) -> LatestPlanResult:
        return self.controllers.plan_snapshots.latest()

    def get_todos(self) -> list[TodoItemWire]:
        return self.controllers.plan_snapshots.todos()

    def dispatch_visualization_action(
        self, entry: VisualizationAction
    ) -> VisualizationActionResult:
        return self.controllers.viz_actions.dispatch(entry)

    def list_background_processes(self) -> list[ProcessRow]:
        return self.controllers.processes.list()

    # ── Workspace trust ───────────────────────────────────────────

    @property
    def workspace_trust(self) -> WorkspaceTrustController:
        """Status and answers for this runtime's folder, read afresh."""
        return WorkspaceTrustController(self.project_dir)

    async def apply_workspace_trust(self) -> None:
        """Rebuild this runtime's session after the folder's trust changed:
        settings reload with or without the folder's config, then everything
        the folder contributes (:meth:`Session.apply_workspace_trust`)."""
        settings = reload_settings(self.project_dir)
        self._settings = settings
        await self._session.apply_workspace_trust(settings)

    # ── Panels + plugins ──────────────────────────────────────────

    @property
    def skill_names(self) -> list[str]:
        return self.controllers.panels.skill_names()

    def get_skill_pool(self) -> SkillPool:
        return self.controllers.panels.skill_pool()

    def get_agent_details(self) -> list[AgentInfo]:
        return self.controllers.panels.agent_details()

    def promote_ephemeral_agent(self, name: str, target: str = "project") -> PromoteEphemeralResult:
        return self.controllers.panels.promote_ephemeral_agent(name, target)

    def get_hooks_details(self) -> list[HookEntryView]:
        return self.controllers.panels.hooks_details()

    def get_skill_details(self) -> list[SkillInfo]:
        return self.controllers.panels.skill_details()

    def get_output_styles(self) -> OutputStylesResult:
        return self.controllers.panels.output_styles()

    def get_group_policy(self) -> GroupPolicyPackResult:
        return self.controllers.panels.group_policy()

    def get_slash_commands(self) -> list[SlashCommandEntry]:
        return self.controllers.panels.slash_commands()

    def get_plugin_details(self) -> list[PluginInfo]:
        return self.controllers.plugins.list_installed()

    def get_marketplaces(self) -> list[MarketplaceInfo]:
        return self.controllers.marketplaces.list_registered()
