"""Session core — the aggregate that holds the sub-systems.

:class:`Session` owns one instance of each sub-system and delegates
every behaviour to it. What it keeps is the session's *state*; what it
does is the small amount of behaviour no collaborator owns — rotating
the identity, reloading the hooks, and the broadcast facade.

Owned concerns (each has a class in this package):

* :class:`~.loop_ops.LoopController` — ``/loop`` state.
* :class:`~.plan_ops.PlanCoordinator` — plan decisions + the research nudge.
* :class:`~.state_ops.RuntimeModeCoordinator` — output-style +
  permission-mode flips.
* :class:`~.output_style_selection.OutputStyleSelection` — the styles and
  the active one.
* :class:`~.compaction.CompactionCoordinator` — auto + manual compaction.
* :class:`~.compaction.ContextBudget` — the window compaction measures
  against and the last run's input tokens.
* :class:`~.startup.SessionStartupCoordinator` — background warmups +
  MCP first-connect.
* :class:`~.mcp_ops.McpLifecycleCoordinator` /
  :class:`~.mcp_ops.McpFailureCache` — MCP connect/disconnect and why a
  connect failed.
* :class:`~.hooks.SessionHooks` — hook loading, the executor and the
  group-hook gate.
* :class:`~.tool_approval.ToolCallApprovalChannel` — putting one tool call
  to the person.
* :class:`~.message_handler.SessionMessageHandler` — the headless message
  pipeline.
* :class:`~.cloud_auth.SessionCloudAuth` — cloud credentials.
* :class:`~.event_log.SessionEventLog` — append-only event log.
* :class:`~.reminders.PendingReminderQueue` — asyncRewake hook buffer.
* :class:`~.tool_hook_factory.ToolEventHookFactory` — ToolEventHook +
  PermissionEvaluator composition.
* :class:`~.learning_ops.SessionLearningManager` — learning inject / extract.
* :class:`~.identity.SessionIdentity` — session_id rotate / rebind.
* :class:`~.plugin_reload.PluginReloadOrchestrator` — hot plugin reload.

The state they are given is declared in :mod:`igni_core.core.session.surface`,
and the construction that fills it is in
:mod:`igni_core.core.session.parts` — one module per concern, each with
one builder class, called in the order the concerns depend on each other
(:class:`~igni_core.core.session.parts.composer.SessionComposer`).

Session persistence and chat history are delegated to Agno's native
``db`` / ``session_id`` mechanism: the main team and all its members
receive the same ``db`` and ``session_id``.
"""

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from igni_core.backend.schemas_model import ModelSwitchResult
from igni_core.core.broadcast_schema import BroadcastEvent, BroadcastSink
from igni_core.core.config.group_policy.revalidation import PolicyRevalidation
from igni_core.core.config.permission_eval import PermissionEvaluator, PermissionMode
from igni_core.core.config.settings import Settings
from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.memory_sync import MemorySyncHook
from igni_core.core.hooks.queue.agno_adapter import AgnoCallableAdapter
from igni_core.core.hooks.tool_hook import ToolEventHook
from igni_core.core.hooks.turn_results import TurnResultHook
from igni_core.core.mcp.hints import ToolHintsRegistry
from igni_core.core.prompts.contributions import PromptContributions
from igni_core.core.prompts.loader import PromptLoader
from igni_core.core.session.event_log_schema import (
    SessionEvent,
    SessionEventAppendResult,
    SessionEventDraft,
)
from igni_core.core.session.parts import SessionComposer, SessionDeps
from igni_core.core.session.parts.agents import AgentsBuilder
from igni_core.core.session.parts.plugins import PluginBuilder
from igni_core.core.session.parts.tooling import ToolingBuilder
from igni_core.core.session.run_debug import RunMessagesDebugDumper
from igni_core.core.session.schemas import (
    MessageMedia,
    PluginReloadCounts,
    SessionLifecyclePayload,
)
from igni_core.core.session.surface import SessionSurface
from igni_core.core.tools.process_supervisor import ProcessSupervisor

logger = logging.getLogger(__name__)


class Session(SessionSurface):
    """Manages a single igni session with all subsystem integrations."""

    #: Fired when a new dialogue begins — set by the backend, the only
    #: layer that can reach the portal. ``/clear`` rotates the session id
    #: in-process and never passes through the session-start RPC.
    #: The hook revalidates the group pack, so it answers with what
    #: that attempt did: the portal being unreachable is an expected
    #: failure and comes back in the result, not as an exception.
    on_new_dialogue: Callable[[], Awaitable[PolicyRevalidation]] | None

    def __init__(
        self,
        settings: Settings,
        project_dir: Path | None = None,
        resume_session_id: str | None = None,
        additional_dirs: list[Path] | None = None,
        contributions: PromptContributions | None = None,
        prompt_loader: PromptLoader | None = None,
        supervisor: ProcessSupervisor | None = None,
        tool_hints: ToolHintsRegistry | None = None,
    ):
        # The live evaluator is the tool-hook factory's; the session
        # mirrors it. There is none until the factory has built one.
        self.permission_evaluator = None
        # The backend installs this; a session nobody drives has none.
        self.on_new_dialogue = None
        SessionComposer(
            self,
            SessionDeps(
                settings=settings,
                project_dir=project_dir,
                resume_session_id=resume_session_id,
                additional_dirs=additional_dirs,
                contributions=contributions,
                prompt_loader=prompt_loader,
                supervisor=supervisor,
                tool_hints=tool_hints,
            ),
        ).compose()

    # ── Rebuilds ───────────────────────────────────────────────

    def reload_group_agents(self) -> bool:
        """Rebuild the pools so a refreshed pack takes effect without a restart.

        The loaders read the pack straight from the cache, so this only
        rebuilds what was constructed from the old contents. Never raises:
        the session that is running matters more than the update.
        """
        try:
            AgentsBuilder(self).build_pools()
            self.rebuild_main_team()
            return True
        except Exception as exc:  # noqa: BLE001 — a live session outranks an update
            logger.warning("Could not reload what the group ships: %s", exc)
            return False

    def rebuild_main_team(self) -> None:
        """Build a fresh main team, and re-resolve the context window.

        ``/model`` comes through here, and compaction must threshold
        against the window the new model actually has.
        """
        AgentsBuilder(self).build_main_team()
        self.context_budget.resize(self.settings)

    def set_default_model(self, model_name: str) -> ModelSwitchResult:
        """Swap the default model if the registry knows it, and rebuild the team."""
        registry = self.settings.models.registry
        if model_name not in registry:
            return ModelSwitchResult(
                ok=False,
                model_name=model_name,
                available=sorted(registry.keys()),
            )
        self.settings.models.default = model_name
        self.rebuild_main_team()
        return ModelSwitchResult(ok=True, model_name=model_name)

    async def fire_session_start_hook(self) -> None:
        """Fire the ``SessionStart`` hook; a hook error must not gate startup."""
        try:
            await self.hook_executor.execute(
                event=HookEvent.SESSION_START.value,
                payload=SessionLifecyclePayload(
                    session_id=self.session_id, resumed=self.session_named
                ),
            )
        except Exception as exc:
            # The hook is the user's own; a broken one is worth saying
            # out loud, even though it must not stop the session.
            logger.warning("session_start hook fire raised: %s", exc)

    def reload_hooks(self) -> int:
        """Reload hooks from the settings files; returns how many are loaded."""
        count = self.hooks.reload()
        PluginBuilder(self).mirror_hooks()
        # The team is the agents builder's: it is the one that knows
        # how a tool-event hook is attached to it.
        AgentsBuilder(self).replace_tool_event_hook(self.tool_event_hook())
        return count

    def reload_plugins(self) -> PluginReloadCounts:
        """Hot-reload plugin contents from disk — no session restart.

        The team is rebuilt here, once: the orchestrator re-applies the
        plugins' contributions, and the team that draws on them is the
        session's own.
        """
        counts = self.plugin_reload_orchestrator.reload()
        self.rebuild_main_team()
        return counts

    async def apply_workspace_trust(self, settings: Settings | None = None) -> None:
        """Pick up a changed workspace-trust decision without a restart.

        Everything a folder brings that runs code — tools, hooks, plugins,
        language servers, monitors — and its ``permissions`` / ``safety``
        config is read through ``core/workspace_trust`` when built, so a
        decision made later only reaches them by building them again.
        ``settings`` is the freshly loaded :class:`Settings`.
        """
        old_mode = self.settings.permissions.mode
        if settings is not None:
            self.settings = settings
        fresh = PermissionEvaluator.from_permissions(self.settings.permissions)
        self.tool_hook_factory.permission_evaluator.adopt(fresh, previous_default=old_mode)

        # Language servers and monitors were started from the old config:
        # stop them before the managers are rebuilt from the new one.
        for manager in (self.lsp_manager, self.monitor_manager):
            try:
                await manager.shutdown_all()
            except Exception as exc:  # noqa: BLE001 — a stuck server must not block the change
                logger.warning(
                    "Could not stop %s on a trust change: %s", type(manager).__name__, exc
                )

        # Plugins, hooks, output styles, pools and plugin MCP configs;
        # then the language-server and monitor managers; then the main
        # team, whose build re-discovers custom tools and picks up the
        # managers. The team is built once, here: the orchestrator
        # re-applies the plugins' contributions, not the team.
        self.plugin_reload_orchestrator.reload()
        ToolingBuilder(self).build()
        self.rebuild_main_team()

    # ── The live permission evaluator ──────────────────────────

    def tool_event_hook(self) -> ToolEventHook:
        """A fresh tool-event hook; also re-reads the live evaluator."""
        self.permission_evaluator = self.tool_hook_factory.permission_evaluator
        return self.tool_hook_factory.create()

    # ── Identity delegation ────────────────────────────────────

    @property
    def session_id(self) -> str:
        """The live id; change it with :meth:`rotate_id`."""
        return self.identity.session_id

    @property
    def session_named(self) -> bool:
        """Whether the session was resumed or renamed; set through :attr:`identity`."""
        return self.identity.session_named

    @property
    def user_id(self) -> str:
        return self.identity.user_id

    def rotate_id(self, new_id: str) -> None:
        self.identity.rotate(new_id)

    async def rebind_identity(self, session_id: str) -> None:
        await self.identity.rebind(session_id)

    @property
    def permission_mode_value(self) -> str:
        """Wire-safe string for the current permission mode (``"default"`` if unknown)."""
        evaluator = self.permission_evaluator
        if evaluator is None:
            return PermissionMode.DEFAULT.value
        return evaluator.mode.value

    def memory_sync_hooks(self) -> list[AgnoCallableAdapter]:
        """The main agent's durability hooks: memory-file writes stored (in
        the cloud), and finished calls' results kept for a turn a restart
        may cut (``core/hooks/turn_results.py``)."""
        hooks: list[AgnoCallableAdapter] = [TurnResultHook(self.sub_agent_hitl)]
        mirror = self.memory_mirror
        if mirror is not None:
            hooks.append(MemorySyncHook(mirror))
        return hooks

    # ── Broadcast facade ───────────────────────────────────────

    def register_broadcast_callback(self, subscriber: BroadcastSink) -> None:
        self.broadcast_bus.register(subscriber)

    async def append_event(self, draft: SessionEventDraft) -> SessionEventAppendResult:
        """Record an event on the session's log and persist it.

        Returns what the append did: the event is in the in-memory
        log either way, but a failed write is reported so the caller
        can decide what a lost event means.
        """
        return await self.event_log_store.append(draft)

    def restore_event_log(self, events: list[SessionEvent]) -> None:
        self.event_log_store.restore(events)

    @property
    def event_log(self) -> list[SessionEvent]:
        """The in-memory event log, oldest first."""
        return self.event_log_store.events

    def broadcast(self, event: BroadcastEvent) -> None:
        self.broadcast_bus.emit(event)

    def queue_post_run_broadcast(self, event: BroadcastEvent) -> None:
        """Defer a broadcast until the current run finishes."""
        self.broadcast_bus.queue_post_run(event)

    def drain_post_run_broadcasts(self, run_id: str | None = None) -> None:
        self.broadcast_bus.drain_post_run(run_id)

    # ── Message handling (headless path) ──────────────────────

    async def handle_message(self, message: str, *, media: MessageMedia | None = None) -> str:
        """Handle a single user message and return the response."""
        result = await self.message_handler.handle(message, media)
        # A side-observer: always safe to fire once the pipeline returns.
        try:
            RunMessagesDebugDumper.dump_team(self.main_team)
        except Exception as exc:  # noqa: BLE001 — a debug dump must not fail the turn
            logger.debug("Could not dump the run messages: %s", exc)
        return result

    @property
    def last_turn_failure(self) -> str:
        """Why the last :meth:`handle_message` failed (already shown to
        the person), or ``""`` when it did not."""
        return self.message_handler.last_failure
