"""The session's declared surface — the sub-systems it holds.

:class:`~igni_core.core.session.core.Session` is an aggregate: it owns
one instance of each sub-system and delegates every behaviour to it.
This module declares that surface in one place, so the list of what a
session holds is one list to read rather than a list to infer from a
constructor.

The declarations are annotations only — no values, no methods — so
nothing is created here. The construction that fills them lives in
:mod:`igni_core.core.session.parts`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from agno.agent import Agent
    from agno.db.base import AsyncBaseDb

    from igni_core.core.agents import AgentPool
    from igni_core.core.agents.tool_check import UnresolvedAgentTools
    from igni_core.core.config.group_policy.layout import GroupPolicyLayout
    from igni_core.core.config.permission_eval import PermissionEvaluator
    from igni_core.core.config.settings import Settings
    from igni_core.core.guardrails.runner import GuardrailRunner
    from igni_core.core.hooks.executor import HookExecutor
    from igni_core.core.hooks.schemas import HookDefinition
    from igni_core.core.loop import LoopProgressStore, LoopStore
    from igni_core.core.lsp import LspServerManager
    from igni_core.core.mcp.client import MCPClientManager
    from igni_core.core.monitors import MonitorManager
    from igni_core.core.plugins import PluginLoader, PluginsState
    from igni_core.core.prompts.contributions import PromptContributions
    from igni_core.core.prompts.loader import PromptLoader
    from igni_core.core.session.broadcast import BroadcastBus
    from igni_core.core.session.cloud_auth import SessionCloudAuth
    from igni_core.core.session.cloud_catalog import CloudModelCatalog
    from igni_core.core.session.compaction import CompactionCoordinator, ContextBudget
    from igni_core.core.session.event_log import SessionEventLog
    from igni_core.core.session.hooks import SessionHooks
    from igni_core.core.session.identity import SessionIdentity
    from igni_core.core.session.learning_ops import SessionLearningManager
    from igni_core.core.session.loop_ops import LoopController
    from igni_core.core.session.mcp_ops import McpFailureCache, McpLifecycleCoordinator
    from igni_core.core.session.memory_ops import SessionMemoryManager
    from igni_core.core.session.message_handler import SessionMessageHandler
    from igni_core.core.session.output_style_selection import OutputStyleSelection
    from igni_core.core.session.persistence import SessionPersistence
    from igni_core.core.session.plan_ops import PlanCoordinator
    from igni_core.core.session.plugin_reload import PluginReloadOrchestrator
    from igni_core.core.session.reminders import PendingReminderQueue
    from igni_core.core.session.startup import SessionStartupCoordinator
    from igni_core.core.session.state_ops import RuntimeModeCoordinator
    from igni_core.core.session.tool_approval import ToolCallApprovalChannel
    from igni_core.core.session.tool_hook_factory import ToolEventHookFactory
    from igni_core.core.skills.loader import SkillPool
    from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator
    from igni_core.core.tools.orchestrate_budget import SpawnBudget
    from igni_core.core.tools.plan import PlanStore
    from igni_core.core.tools.process_supervisor import ProcessSupervisor
    from igni_core.core.tools.todo import TodoStore
    from igni_core.core.utils.audit import AuditLogger
    from igni_core.core.utils.display import DisplayManager
    from igni_core.core.utils.memory_files import MemoryMirror
    from igni_core.core.utils.rules_index import RulesIndex
    from igni_core.core.workspace import WorkspaceManager


class SessionSurface:
    """The sub-systems a :class:`Session` holds.

    Purely declarative: the annotations are the contract, and the
    construction that satisfies them lives in
    :mod:`igni_core.core.session.parts`.
    """

    # ── What the session was given ────────────────────────────

    settings: Settings
    supervisor: ProcessSupervisor
    prompt_contributions: PromptContributions
    prompt_loader: PromptLoader
    cloud_catalog: CloudModelCatalog

    # ── Where the session runs ─────────────────────────────────

    project_dir: Path
    workspace: WorkspaceManager
    group_policy: GroupPolicyLayout

    # ── The scratch the tools write into ───────────────────────

    loop_store: LoopStore
    loop_progress_store: LoopProgressStore
    loop: LoopController
    todo_store: TodoStore
    plan_store: PlanStore
    memory_mirror: MemoryMirror | None
    output_style: OutputStyleSelection
    broadcast_bus: BroadcastBus
    reminders: PendingReminderQueue
    event_log_store: SessionEventLog

    # ── The project's own context ──────────────────────────────

    project_instructions: str
    rules_index: RulesIndex

    # ── Plugins, hooks and the tools they wire ────────────────

    plugin_state: PluginsState
    plugin_loader: PluginLoader
    disabled_plugins: set[str]
    hooks: SessionHooks
    hook_executor: HookExecutor
    hooks_map: dict[str, list[HookDefinition]]
    tool_hook_factory: ToolEventHookFactory
    #: The live evaluator. ``None`` until the tool-hook factory has
    #: built one — the factory is seeded with it, so an empty cache
    #: never rebuilds it from the settings.
    permission_evaluator: PermissionEvaluator | None

    # ── The agents ─────────────────────────────────────────────

    pool: AgentPool
    skill_pool: SkillPool
    spawn_budget: SpawnBudget
    agent_tool_check: UnresolvedAgentTools
    main_team: Agent

    # ── The MCP servers, the language servers and the monitors ─

    mcp_manager: MCPClientManager
    mcp_failures: McpFailureCache
    mcp_lifecycle: McpLifecycleCoordinator
    lsp_manager: LspServerManager
    monitor_manager: MonitorManager

    # ── What the session persists, and what it learns ──────────

    db: AsyncBaseDb
    persistence: SessionPersistence
    memory_mgr: SessionMemoryManager
    learning_mgr: SessionLearningManager

    # ── The collaborators that own the behaviour ───────────────

    audit: AuditLogger
    display: DisplayManager
    sub_agent_hitl: SubAgentHITLCoordinator
    approval: ToolCallApprovalChannel
    guardrail_runner: GuardrailRunner
    context_budget: ContextBudget
    cloud_auth: SessionCloudAuth
    identity: SessionIdentity
    mode: RuntimeModeCoordinator
    plan: PlanCoordinator
    compaction: CompactionCoordinator
    startup: SessionStartupCoordinator
    plugin_reload_orchestrator: PluginReloadOrchestrator
    message_handler: SessionMessageHandler


__all__ = ["SessionSurface"]
