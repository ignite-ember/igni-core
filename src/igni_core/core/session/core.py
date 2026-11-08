"""Session core — wires up subsystems and delegates to coordinators.

:class:`Session` constructs the subsystem instances, composes the classes
that own each concern's state and behaviour, and exposes a thin public
surface that forwards to them.

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

Session persistence and chat history are delegated to Agno's native
``db`` / ``session_id`` mechanism: the main team and all its members
receive the same ``db`` and ``session_id``.
"""

import contextlib
import getpass
import logging
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from agno.agent import Agent

from igni_core.backend.schemas_model import ModelSwitchResult
from igni_core.core.agents import AgentContributionApplier, AgentPool
from igni_core.core.agents.tool_check import UnresolvedAgentTools
from igni_core.core.auth.credentials import CloudCredentials
from igni_core.core.broadcast_schema import BroadcastEvent, BroadcastSink
from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.config.models import ModelRegistry
from igni_core.core.config.permission_eval import PermissionEvaluator
from igni_core.core.config.settings import Settings
from igni_core.core.config.tool_permissions import ToolPermissions
from igni_core.core.guardrails.runner import GuardrailRunner
from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.hooks.memory_sync import MemorySyncHook
from igni_core.core.hooks.schemas import HookDefinition
from igni_core.core.hooks.tool_hook import ToolEventHook
from igni_core.core.hooks.turn_results import TurnResultHook
from igni_core.core.init import InitConfig, ProjectInitializer
from igni_core.core.learn import create_learning_machine
from igni_core.core.loop import LoopProgressStore, LoopStore, LoopToolResult
from igni_core.core.lsp import LspServerManager, load_lsp_config
from igni_core.core.mcp.client import MCPClientManager
from igni_core.core.mcp.config import MCPConfigLoader
from igni_core.core.mcp.hints import ToolHintsRegistry
from igni_core.core.memory.manager import StorageManager
from igni_core.core.monitors import MonitorConfigLoader, MonitorManager
from igni_core.core.network_policy import NetworkPolicy
from igni_core.core.output_styles import OutputStyle, discover_output_styles
from igni_core.core.plugins import PluginLoader, load_state
from igni_core.core.prompts import load_prompt
from igni_core.core.prompts.contributions import prompt_contributions
from igni_core.core.session.agent_builder import MainAgentBuilder
from igni_core.core.session.agent_factory import (
    create_guardrails,
    create_reasoning_tools,
)
from igni_core.core.session.broadcast import BroadcastBus
from igni_core.core.session.cloud_auth import SessionCloudAuth
from igni_core.core.session.cloud_catalog import CloudModelCatalog
from igni_core.core.session.compaction import (
    CompactionCoordinator,
    ContextBudget,
    TeamSessionSource,
)
from igni_core.core.session.compression import MeasuredCompression
from igni_core.core.session.event_log import SessionEventLog
from igni_core.core.session.event_log_schema import SessionEvent, SessionEventDraft
from igni_core.core.session.hooks import SessionHooks
from igni_core.core.session.identity import SessionIdentity
from igni_core.core.session.learning_ops import SessionLearningManager
from igni_core.core.session.loop_ops import LoopController
from igni_core.core.session.mcp_ops import (
    McpFailureCache,
    McpLifecycleCoordinator,
    McpLifecycleDeps,
)
from igni_core.core.session.mcp_resolver import MCPToolResolver
from igni_core.core.session.memory_ops import SessionMemoryManager
from igni_core.core.session.message_handler import SessionMessageHandler
from igni_core.core.session.output_style_selection import OutputStyleSelection
from igni_core.core.session.persistence import SessionPersistence
from igni_core.core.session.plan_ops import PlanCoordinator, PlanDeps
from igni_core.core.session.plugin_reload import PluginReloadOrchestrator
from igni_core.core.session.reminders import PendingReminderQueue
from igni_core.core.session.run_debug import RunMessagesDebugDumper
from igni_core.core.session.schemas import (
    CompactResult,
    ContextBreakdown,
    LoopAdvance,
    McpInitResult,
    MessageMedia,
    PermissionModeSwitch,
    PlanDecisionResult,
    PluginReloadCounts,
    SessionLifecyclePayload,
)
from igni_core.core.session.startup import (
    MarketplaceWarmupPhase,
    McpInitDeps,
    McpInitPhase,
    SessionStartupCoordinator,
)
from igni_core.core.session.state_ops import RuntimeModeCoordinator, RuntimeModeDeps
from igni_core.core.session.tool_approval import ToolCallApprovalChannel
from igni_core.core.session.tool_hook_factory import ToolEventHookFactory
from igni_core.core.skills.loader import SkillPool
from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator
from igni_core.core.tools.orchestrate_budget import SpawnBudget
from igni_core.core.tools.plan import PlanStore
from igni_core.core.tools.registry import ToolRegistry
from igni_core.core.tools.todo import TodoStore
from igni_core.core.utils.audit import AuditLogger
from igni_core.core.utils.context import ProjectMemoryBank, load_project_context
from igni_core.core.utils.display import DisplayManager
from igni_core.core.utils.memory_files import MemoryMirror
from igni_core.core.utils.rules_index import RulesIndex
from igni_core.core.workspace import WorkspaceManager
from igni_core.core.workspace_trust import WorkspaceTrust

logger = logging.getLogger(__name__)


class Session:
    """Manages a single igni session with all subsystem integrations."""

    def __init__(
        self,
        settings: Settings,
        project_dir: Path | None = None,
        resume_session_id: str | None = None,
        additional_dirs: list[Path] | None = None,
    ):
        self.settings = settings

        # Every code path below may read ``settings.models.default``, so
        # the cloud catalog is refreshed first: a brand-new install reaches
        # a usable state right after login without a hardcoded fallback.
        self.cloud_catalog = CloudModelCatalog(settings)
        self.cloud_catalog.refresh()

        self.project_dir = project_dir or Path.cwd()
        self.workspace = WorkspaceManager(self.project_dir, additional_dirs)

        self._init_loop_state()
        self._init_per_session_scratch()

        #: Where the group-policy cache keeps each kind's entries.
        self.group_policy = GroupPolicyLayout.for_data_dir(settings.storage.data_dir)

        # The bundled agents stand down when the group ships its own —
        # otherwise this would scaffold back the very agents an admin
        # removed, and two sources would fight over one checksum file.
        ProjectInitializer(
            project_dir=self.project_dir,
            trust=WorkspaceTrust.shared(),
            config=InitConfig(
                skip_bundled_agents=self.group_policy.ships("agents"),
                skip_builtin_hook_registration=self.group_policy.ships("hooks"),
                group_ships_hook_scripts=self.group_policy.ships("scripts"),
            ),
        ).run()

        self.db = StorageManager.build_db(settings, project_dir=self.project_dir)
        self.audit = AuditLogger(settings)
        self.display = DisplayManager()

        # Sub-agents spawned by the orchestrator pause inside the parent's
        # tool execution; this coordinator carries those pauses (and the
        # session's own single-call questions) to the person.
        self.sub_agent_hitl = SubAgentHITLCoordinator()
        self.approval = ToolCallApprovalChannel(self.sub_agent_hitl)

        #: Fired when a new dialogue begins — set by the backend, the only
        #: layer that can reach the portal. ``/clear`` rotates the session id
        #: in-process and never passes through the session-start RPC.
        self.on_new_dialogue: Callable[[], Awaitable[Any]] | None = None

        #: The live evaluator, re-read from the tool-hook factory on every build.
        self.permission_evaluator: PermissionEvaluator | None = None
        self.hooks = SessionHooks(
            project_dir=self.project_dir,
            settings=settings,
            group_hooks_dir=self.group_policy.dir_for("hooks"),
            reminders=self.reminders,
            resolver=MCPToolResolver(mcp_manager_ref=lambda: self.mcp_manager),
            ask=self.approval.ask,
        )

        self._init_project_context(settings)
        self._init_plugins_output_styles_hooks(settings)
        self._init_agent_and_skill_pools(settings)

        self.context_budget = ContextBudget.for_settings(settings)

        # Composed before ``main_team`` so the first agent build can read
        # the access token; every credential change rebuilds the team.
        self.cloud_auth = SessionCloudAuth(
            creds=CloudCredentials(settings.auth.credentials_file),
            server_url=settings.api_url,
            rebuild_team=self.rebuild_main_team,
            catalog=self.cloud_catalog,
        )

        self.identity = SessionIdentity(
            session_id=resume_session_id or str(uuid.uuid4())[:8],
            session_named=bool(resume_session_id),
            # In the cloud, Agno's rows are told apart by ``user_id``,
            # which the scoped db fixes to the session's scope.
            user_id=self._scoped_user_id() or getpass.getuser(),
            main_team_ref=lambda: getattr(self, "main_team", None),
            persistence_ref=lambda: getattr(self, "persistence", None),
        )

        self._init_mcp_client_manager()
        self._init_lsp_and_monitors()

        self.guardrail_runner = GuardrailRunner(settings)

        self.persistence = SessionPersistence(self.db, self.session_id)
        self.memory_mgr = SessionMemoryManager(self.db, settings, self.user_id)

        boot = create_learning_machine(settings, self.db)
        self.learning_mgr = SessionLearningManager(
            settings=settings,
            db=self.db,
            user_id_ref=lambda: self.user_id,
            session_id_ref=lambda: self.session_id,
            main_team_ref=lambda: self.main_team,
            learning=boot.machine,
            boot_reason=boot.reason,
        )

        self._init_coordinators()
        self.mcp_lifecycle = McpLifecycleCoordinator(
            McpLifecycleDeps(
                mcp_manager=self.mcp_manager,
                rebuild=lambda: self.rebuild_mcp(),
            )
        )
        self.plugin_reload_orchestrator = PluginReloadOrchestrator(
            project_dir=self.project_dir,
            mcp_manager=self.mcp_manager,
            plugin_loader_ref=lambda: self.plugin_loader,
            disabled_plugins_ref=lambda: self._disabled_plugins,
            rebuild_plugins_and_hooks=lambda: self._init_plugins_output_styles_hooks(self.settings),
            rebuild_agent_and_skill_pools=lambda: self._init_agent_and_skill_pools(self.settings),
            rebuild_main_team=self.rebuild_main_team,
            skill_pool_ref=lambda: self.skill_pool,
            agent_pool_ref=lambda: self.pool,
            hooks_map_ref=lambda: self.hooks.hooks_map,
            disconnect_removed_mcps=self.mcp_lifecycle.disconnect,
            auto_connect_mcps=self.mcp_lifecycle.connect,
        )

        self.main_team = self._build_main_agent()

        # Built once and never rebuilt, so everything the session can change
        # under it is handed over as a stable owner or a callable.
        self._message_handler = SessionMessageHandler(
            hooks=self.hooks,
            audit=self.audit,
            display=self.display,
            guardrail_runner=self.guardrail_runner,
            team_ref=lambda: self.main_team,
            pending_reminders_drain=self.reminders.drain,
            compact_hook=self.compact_if_needed,
            ensure_mcp=self.ensure_mcp,
            session_id_ref=lambda: self.session_id,
            budget=self.context_budget,
            # Replaced with the tool-hook factory on every plugin reload.
            permission_evaluator_ref=lambda: self.permission_evaluator,
        )

    # ── __init__ helpers ────────────────────────────────────────

    def _init_coordinators(self) -> None:
        """The mode, plan, compaction and startup coordinators.

        Each is handed only what it reads. What the session replaces
        while it runs (the main team, the evaluator, the pool, the
        settings, the id) goes in as a callable read on every use.
        """
        self.mode = RuntimeModeCoordinator(
            RuntimeModeDeps(
                evaluator_ref=lambda: self.permission_evaluator,
                output_style=self.output_style,
                team_ref=lambda: self.main_team,
                bus=self.broadcast_bus,
                permissions=self.settings.permissions,
            )
        )
        self.plan = PlanCoordinator(
            PlanDeps(
                plan_store=self.plan_store,
                persistence=self.persistence,
                bus=self.broadcast_bus,
                set_permission_mode=self.mode.set_permission_mode,
                starting_mode=lambda: self.mode.starting_mode,
            )
        )
        self.compaction = CompactionCoordinator(
            TeamSessionSource(
                team_ref=lambda: self.main_team,
                session_id_ref=lambda: self.session_id,
                user_id_ref=lambda: self.user_id,
            ),
            hook_executor_ref=lambda: self.hook_executor,
            rebuild_team=lambda: self.rebuild_main_team(),
        )
        self.startup = SessionStartupCoordinator(
            MarketplaceWarmupPhase(settings_ref=lambda: self.settings),
            McpInitPhase(
                McpInitDeps(
                    mcp_manager=self.mcp_manager,
                    failures=self.mcp_failures,
                    display=self.display,
                    pool_ref=lambda: self.pool,
                    rebuild_team=lambda: self.rebuild_main_team(),
                )
            ),
        )

    def _scoped_user_id(self) -> str | None:
        """The scope a shared database's Agno storage fixes ``user_id`` to, if it does."""
        scoped = getattr(self.db, "scoped_user_id", None)
        return scoped if isinstance(scoped, str) and scoped else None

    def _init_per_session_scratch(self) -> None:
        """The per-session stores populated by tools and the run loop."""
        self.todo_store = TodoStore()
        self.plan_store = PlanStore()
        memory_bank = ProjectMemoryBank(self.project_dir)
        memory_bank.ensure()
        # In the cloud the memory directory is a write-through mirror of
        # the database's rows; ``None`` on the desktop, whose files are the store.
        self.memory_mirror: MemoryMirror | None = memory_bank.mirror()
        self.output_style = OutputStyleSelection()
        self.broadcast_bus = BroadcastBus()
        self.reminders = PendingReminderQueue()
        self.event_log_store = SessionEventLog(
            persist_ref=lambda: getattr(self, "persistence", None)
        )

    def _init_project_context(self, settings: Settings) -> None:
        """Top-level project instructions + the :class:`RulesIndex`.

        Subdirectory rules are discovered lazily by :class:`ToolEventHook`.
        """
        self.project_instructions = load_project_context(
            self.project_dir,
            settings.context.project_file,
            read_claude_md=settings.rules.cross_tool_support,
        )
        self.rules_index = RulesIndex(
            self.project_dir,
            read_claude_md=settings.rules.cross_tool_support,
            group_rules_dir=self.group_policy.shipped_dir("rules"),
        )

    def _init_loop_state(self) -> None:
        self.loop_store = LoopStore(project_dir=self.project_dir)
        self.loop_progress_store = LoopProgressStore(project_dir=self.project_dir)
        self.loop = LoopController(self.loop_store)

    def _init_mcp_client_manager(self) -> None:
        """The MCP client manager, with plugin-bundled configs merged in."""
        self.mcp_manager = MCPClientManager(
            self.project_dir,
            group_mcps_dir=self.group_policy.dir_for("mcps"),
            network_policy=NetworkPolicy.from_settings(self.settings),
        )
        self.mcp_failures = McpFailureCache()
        self.plugin_loader.apply_to_mcp(
            MCPConfigLoader(self.project_dir),
            self.mcp_manager.configs,
            disabled=self._disabled_plugins,
        )

    def reload_group_agents(self) -> bool:
        """Rebuild the pools so a refreshed pack takes effect without a restart.

        The loaders read the pack straight from the cache, so this only
        rebuilds what was constructed from the old contents. Never raises:
        the session that is running matters more than the update.
        """
        try:
            self._init_agent_and_skill_pools(self.settings)
            self.rebuild_main_team()
            return True
        except Exception as exc:  # noqa: BLE001 — a live session outranks an update
            logger.warning("Could not reload what the group ships: %s", exc)
            return False

    def _init_agent_and_skill_pools(self, settings: Settings) -> None:
        """The agent and skill pools from the current plugin set."""
        self.pool = AgentPool(db=self.db, broadcast=self.broadcast)
        self.pool.load_definitions(
            settings,
            self.project_dir,
            group_dir=self.group_policy.shipped_dir("agents"),
        )
        self.plugin_loader.apply_to_agents(self.pool, disabled=self._disabled_plugins)
        AgentContributionApplier(prompt_contributions).apply_to(self.pool)
        # One sub-agent cap for the session, drawn on by every
        # OrchestrateTools at every depth.
        self.spawn_budget = SpawnBudget(settings.orchestration.max_total_agents)
        if settings.orchestration.generate_ephemeral:
            self.pool.init_ephemeral(
                self.project_dir, settings.orchestration.max_ephemeral_per_session
            )
        self.pool.build_agents()
        # Checked once the pools are built: custom Python tools register
        # when discovered, and asking earlier would call them typos.
        self.agent_tool_check = self._check_agent_tools()
        self.agent_tool_check.log_warnings()

        self.skill_pool = SkillPool()
        self.skill_pool.load_all(
            self.project_dir,
            settings.skills.cross_tool_support,
            group_dir=self.group_policy.shipped_dir("skills"),
        )
        self.plugin_loader.apply_to_skills(self.skill_pool, disabled=self._disabled_plugins)

    def _check_agent_tools(self) -> UnresolvedAgentTools:
        try:
            return UnresolvedAgentTools.check(
                self.pool.list_agents(), ToolRegistry(base_dir=str(self.project_dir))
            )
        except Exception as exc:  # noqa: BLE001 — a diagnostic must not stop a start
            logger.debug("Could not check agent tools: %s", exc)
            return UnresolvedAgentTools()

    def _init_lsp_and_monitors(self) -> None:
        """The language-server manager (lazy) and monitor manager from the plugin set."""
        lsp_configs = load_lsp_config(
            self.project_dir,
            plugin_roots=self.plugin_loader.collect_lsp_roots(disabled=self._disabled_plugins),
        )
        self.lsp_manager = LspServerManager(lsp_configs, self.project_dir)

        monitor_configs = MonitorConfigLoader(
            self.project_dir,
            trust=WorkspaceTrust.shared(),
            plugin_roots=self.plugin_loader.collect_monitor_roots(disabled=self._disabled_plugins),
        ).load()
        self.monitor_manager = MonitorManager(monitor_configs, self.project_dir)

    def _init_plugins_output_styles_hooks(self, settings: Settings) -> None:
        """Plugins, then output styles, then hooks and the tool-hook factory.

        Also runs on every plugin hot-reload.
        """
        self.plugin_state = load_state(settings.storage.data_dir)
        self.plugin_loader = PluginLoader()
        self.plugin_loader.load_all(self.project_dir)
        managed_plugins = {p.name for p in self.plugin_loader.list_plugins() if p.is_managed}
        self._disabled_plugins = set(self.plugin_state.disabled) - managed_plugins

        plugin_style_roots = [
            (p.root_path, p.name)
            for p in self.plugin_loader.list_plugins()
            if p.name not in self._disabled_plugins
        ]
        self.output_style.adopt(
            discover_output_styles(
                self.project_dir,
                plugin_roots=plugin_style_roots,
                read_claude=settings.rules.cross_tool_support,
                group_dir=self.group_policy.shipped_dir("output-styles"),
            )
        )

        self.hooks.load(settings, self.plugin_loader, self._disabled_plugins)
        self._mirror_hooks()

        # Seeded with the live evaluator: an empty cache would rebuild it
        # from ``settings.permissions.mode`` and take the session out of
        # plan mode on ``/plugin install``.
        factory = ToolEventHookFactory(
            settings=settings,
            rules_index=self.rules_index,
            project_dir=self.project_dir,
            hooks=self.hooks,
            session_id_ref=lambda: self.session_id,
            permission_evaluator=self.permission_evaluator,
        )
        factory.bypass_guard = factory.build_bypass_guard(
            self.approval.ask,
            # Read per call: ``apply_workspace_trust`` replaces ``settings``.
            model_ref=lambda: ModelRegistry(self.settings).get_model(),
            # The process's registry, which the extension installer fills.
            tool_hints=ToolHintsRegistry.shared(),
        )
        factory.ask_person = self.approval.ask
        self._tool_hook_factory = factory
        self.permission_evaluator = factory.permission_evaluator

    def _mirror_hooks(self) -> None:
        """Expose the current executor and hook map on the session."""
        self.hook_executor: HookExecutor = self.hooks.executor
        self.hooks_map: dict[str, list[HookDefinition]] = self.hooks.hooks_map

    # ── Cloud auth accessors (forward to SessionCloudAuth) ─────

    @property
    def cloud_connected(self) -> bool:
        """Whether the session is authenticated with an igni server."""
        return self.cloud_auth.connected

    @property
    def cloud_email(self) -> str | None:
        """The signed-in account's address."""
        return self.cloud_auth.email

    @property
    def cloud_org_id(self) -> str | None:
        """The organization ID from the igni server JWT."""
        return self.cloud_auth.org_id

    @property
    def cloud_org_name(self) -> str | None:
        """The organization display name from the igni server JWT."""
        return self.cloud_auth.org_name

    def replace_cloud_credentials(self, creds: CloudCredentials) -> None:
        self.cloud_auth.replace(creds)

    def clear_cloud_credentials(self) -> None:
        self.cloud_auth.clear()

    def refresh_cloud_models(self) -> int:
        return self.cloud_auth.refresh_models()

    # ── Rebuilds ───────────────────────────────────────────────

    def rebuild_main_team(self) -> None:
        """Build a fresh main team, and re-resolve the context window.

        ``/model`` comes through here, and compaction must threshold
        against the window the new model actually has.
        """
        self.main_team = self._build_main_agent()
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
            logger.debug("session_start hook fire raised: %s", exc)

    def reload_hooks(self) -> int:
        """Reload hooks from the settings files; returns how many are loaded."""
        count = self.hooks.reload()
        self._mirror_hooks()
        tool_event_hook = self.tool_event_hook()
        if self.main_team:
            existing = self.main_team.tool_hooks or []
            self.main_team.tool_hooks = [h for h in existing if not isinstance(h, ToolEventHook)]
            self.main_team.tool_hooks.append(tool_event_hook)
        return count

    def reload_plugins(self) -> PluginReloadCounts:
        """Hot-reload plugin contents from disk — no session restart."""
        return self.plugin_reload_orchestrator.reload()

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
        self._tool_hook_factory.permission_evaluator.adopt(fresh, previous_default=old_mode)

        # Language servers and monitors were started from the old config:
        # stop them before the managers are rebuilt from the new one.
        for manager in (self.lsp_manager, self.monitor_manager):
            try:
                await manager.shutdown_all()
            except Exception as exc:  # noqa: BLE001 — a stuck server must not block the change
                logger.warning(
                    "Could not stop %s on a trust change: %s", type(manager).__name__, exc
                )

        # Plugins, hooks, output styles, pools and plugin MCP configs; then
        # the language-server and monitor managers; then the main team,
        # whose build re-discovers custom tools and picks up the managers.
        self.plugin_reload_orchestrator.reload()
        self._init_lsp_and_monitors()
        self.rebuild_main_team()

    # ── Main Agent setup ────────────────────────────────────────

    def _build_main_agent(self) -> Agent:
        return MainAgentBuilder(
            self,
            agent_cls=Agent,
            registry_cls=ToolRegistry,
            permissions_cls=ToolPermissions,
            compression_cls=MeasuredCompression,
            model_registry_cls=ModelRegistry,
            reasoning_factory=create_reasoning_tools,
            guardrails_factory=create_guardrails,
            prompt_loader=load_prompt,
        ).build()

    # ── Public accessors consumed by the agent-builder sub-package ──

    @property
    def cloud_access_token(self) -> str | None:
        """The igni server access token (``None`` when logged out)."""
        return self.cloud_auth.access_token

    @property
    def cloud_server_url(self) -> str:
        """The igni server API root URL used by cloud-routed tools."""
        return self.cloud_auth.server_url

    @property
    def output_styles(self) -> dict[str, OutputStyle]:
        return self.output_style.styles

    @property
    def active_output_style(self) -> str:
        """Name of the currently-active output style (or empty string)."""
        return self.output_style.active

    def set_active_output_style(self, name: str) -> None:
        self.output_style.select(name)

    @property
    def disabled_plugins(self) -> set[str]:
        """Plugin names currently disabled by ``plugin_state``."""
        return self._disabled_plugins

    @property
    def plugin_data_dir(self) -> str:
        """Root directory for the plugin registry / installer / state, read live."""
        return self.settings.storage.data_dir

    @property
    def learning(self) -> Any:
        """The Agno :class:`LearningMachine`, or ``None`` when learning is off."""
        return self.learning_mgr.machine

    @property
    def learning_machine(self) -> Any:
        """Effective Learning Machine for user-facing recall commands."""
        return self.learning_mgr.effective_machine

    def tool_event_hook(self) -> ToolEventHook:
        """A fresh tool-event hook; also re-reads the live evaluator."""
        self.permission_evaluator = self._tool_hook_factory.permission_evaluator
        return self._tool_hook_factory.create()

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

    # ── Learning delegation ────────────────────────────────────

    async def inject_learnings(self) -> None:
        await self.learning_mgr.inject()

    async def extract_learnings(self, user_msg: str, assistant_msg: str) -> None:
        await self.learning_mgr.extract(user_msg, assistant_msg)

    @property
    def permission_mode_value(self) -> str:
        """Wire-safe string for the current permission mode (``"default"`` if unknown)."""
        evaluator = getattr(self, "permission_evaluator", None)
        raw_mode = getattr(evaluator, "mode", None)
        raw_val = getattr(raw_mode, "value", raw_mode)
        return raw_val if isinstance(raw_val, str) else "default"

    def memory_sync_hooks(self) -> list[Any]:
        """The main agent's durability hooks: memory-file writes stored (in
        the cloud), and finished calls' results kept for a turn a restart
        may cut (``core/hooks/turn_results.py``)."""
        hooks: list[Any] = [TurnResultHook(self.sub_agent_hitl)]
        mirror = getattr(self, "memory_mirror", None)
        if mirror is not None:
            hooks.append(MemorySyncHook(mirror))
        return hooks

    # ── Runtime-mode delegation (output style + permission mode) ──

    def set_output_style(self, name: str) -> str:
        return self.mode.set_output_style(name)

    def set_permission_mode(self, mode: str) -> PermissionModeSwitch:
        return self.mode.set_permission_mode(mode)

    # ── Plan decision delegation ────────────────────────────────

    async def approve_plan(self, run_id: str) -> PlanDecisionResult:
        return await self.plan.approve(run_id)

    async def dismiss_plan(self, run_id: str) -> PlanDecisionResult:
        return await self.plan.dismiss(run_id)

    # ── Broadcast facade ───────────────────────────────────────

    def register_broadcast_callback(self, subscriber: BroadcastSink) -> None:
        self.broadcast_bus.register(subscriber)

    async def append_event(self, draft: SessionEventDraft) -> None:
        await self.event_log_store.append(draft)

    def restore_event_log(self, events: list[SessionEvent]) -> None:
        self.event_log_store.restore(events)

    @property
    def event_log(self) -> list[SessionEvent]:
        """Live reference to the in-memory event log."""
        return self.event_log_store.events

    def broadcast(self, event: BroadcastEvent) -> None:
        self.broadcast_bus.emit(event)

    def queue_post_run_broadcast(self, event: BroadcastEvent) -> None:
        """Defer a broadcast until the current run finishes."""
        self.broadcast_bus.queue_post_run(event)

    def drain_post_run_broadcasts(self, run_id: str | None = None) -> None:
        self.broadcast_bus.drain_post_run(run_id)

    # ── Loop-state proxies + delegation (thin wrappers over LoopController) ──

    @property
    def pending_loop_prompt(self) -> str | None:
        """The active iteration prompt, or ``None`` when idle."""
        return self.loop.pending_loop_prompt

    @property
    def loop_iteration_index(self) -> int:
        """1-based counter of iterations dispatched."""
        return self.loop.loop_iteration_index

    @property
    def loop_iterations_remaining(self) -> int:
        return self.loop.loop_iterations_remaining

    @property
    def loop_run_id(self) -> str | None:
        """UUID scoping :class:`LoopProgressStore` writes."""
        return self.loop.loop_run_id

    @property
    def loop_cap_explicit(self) -> bool:
        """Whether the user typed ``/loop N <prompt>``."""
        return self.loop.loop_cap_explicit

    @property
    def loop_paused(self) -> bool:
        return self.loop.paused

    async def load_persisted_loop_state(self) -> None:
        await self.loop.load_persisted_loop_state()

    async def start_loop(
        self,
        prompt: str,
        max_iter: int,
        *,
        immediate: bool,
        cap_explicit: bool,
    ) -> str:
        return await self.loop.start_loop(
            prompt, max_iter, immediate=immediate, cap_explicit=cap_explicit
        )

    async def advance_loop(self) -> LoopAdvance | None:
        return await self.loop.advance_loop()

    async def cancel_loop(self) -> bool:
        return await self.loop.cancel_loop()

    async def pause_loop(self) -> bool:
        return await self.loop.pause_loop()

    async def resume_loop(self) -> str | None:
        return await self.loop.resume_loop()

    # ── Loop tool-facing delegators (used by LoopTools) ────────

    async def start_loop_from_tool(self, prompt: str, max_iterations: int) -> LoopToolResult:
        return await self.loop.start_loop_from_tool(prompt, max_iterations)

    async def stop_loop_from_tool(self) -> LoopToolResult:
        return await self.loop.stop_loop_from_tool()

    async def set_announced_total_from_tool(self, total: int) -> LoopToolResult:
        """Threads the session-owned :class:`LoopProgressStore` to the controller."""
        return await self.loop.set_announced_total_from_tool(total, self.loop_progress_store)

    async def resume_loop_from_tool(self) -> LoopToolResult:
        return await self.loop.resume_loop_from_tool()

    def loop_status_from_tool(self) -> LoopToolResult:
        return self.loop.loop_status_from_tool()

    # ── Marketplace / MCP startup ──────

    def start_marketplace_refresh_background(self) -> None:
        self.startup.start_marketplace_refresh_background()

    async def ensure_mcp(self) -> McpInitResult:
        return await self.startup.ensure_mcp()

    def rebuild_mcp(self) -> None:
        self.startup.rebuild_mcp()

    # ── Dynamic context compaction ─────────────────────────────

    async def compact_if_needed(self, input_tokens: int, context_window: int) -> bool:
        return await self.compaction.compact_if_needed(input_tokens, context_window)

    async def force_compact(self) -> CompactResult:
        return await self.compaction.force_compact()

    async def context_breakdown(self) -> ContextBreakdown:
        return await self.compaction.context_breakdown()

    # ── Message handling (headless path) ──────────────────────

    async def handle_message(self, message: str, *, media: MessageMedia | None = None) -> str:
        """Handle a single user message and return the response."""
        result = await self._message_handler.handle(message, media)
        # A side-observer: always safe to fire once the pipeline returns.
        with contextlib.suppress(Exception):
            RunMessagesDebugDumper.dump_team(self.main_team)
        return result

    @property
    def last_turn_failure(self) -> str:
        """Why the last :meth:`handle_message` failed (already shown to
        the person), or ``""`` when it did not."""
        return self._message_handler.last_failure
