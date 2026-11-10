"""Typed dependency contracts for the main-agent tools build.

The tools builder used to take twenty keyword parameters and keep
twenty-one fields — one per collaborator the session owned — and it
took the whole :class:`Session` *alongside* attributes copied off
that same session. So the "session-free" claim in its docstring was
never true, and the constructor was a list of collaborators rather
than a description of what the builder needs.

Two problems, one fix. The collaborators now arrive in named bags
split by concern, so a new collaborator is a new field on the bag
that owns it rather than a twenty-first constructor argument; and the
session never arrives at all.

* :class:`CloudCredentials` — the cloud account the cloud-routed
  tools talk to.
* :class:`ToolRegistryDeps` — what the built-in tool registry needs.
* :class:`ToolSourceDeps` — where custom toolkits come from.
* :class:`OrchestrationDeps` — the delegation surface.
* :class:`SessionToolkits` — the toolkits that answer back into a
  live session. They cannot be built from collaborators alone (they
  reach into the session's stores), so the coordinator — which owns
  the session — builds them and hands them over finished.
* :class:`ToolBuildDeps` — the top bag the builder receives.

The bags are frozen dataclasses, like the sibling ports in
:mod:`igni_core.core.session` (:class:`~igni_core.core.session.plan_ops.PlanDeps`,
:class:`~igni_core.core.session.state_ops.RuntimeModeDeps`,
:class:`~igni_core.core.session.mcp_ops.McpLifecycleDeps`). They
describe collaborators, not data: a Pydantic model would type-check
every collaborator at runtime and refuse the module-alias patch seam
the session's own tests rely on (``patch("igni_core.core.session.core.ToolRegistry")``
and friends), which is a contract the builder must not break.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agno.tools import Toolkit

from igni_core.core.agents import AgentPool
from igni_core.core.broadcast_schema import BroadcastSink
from igni_core.core.config.settings import Settings
from igni_core.core.config.tool_permissions import ToolPermissions
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.lsp import LspServerManager
from igni_core.core.mcp.client import MCPClientManager
from igni_core.core.monitors import MonitorManager
from igni_core.core.plugins import PluginLoader
from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator
from igni_core.core.tools.loop import LoopTools
from igni_core.core.tools.loop_progress import LoopProgressTool
from igni_core.core.tools.orchestrate_budget import SpawnBudget
from igni_core.core.tools.plan import PlanTool
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.tools.registry import ToolRegistry
from igni_core.core.tools.slash import SlashCommandTool
from igni_core.core.tools.todo import TodoTools

if TYPE_CHECKING:
    from igni_core.core.session.core import Session


@dataclass(frozen=True)
class CloudCredentials:
    """The cloud account the cloud-routed tools talk to.

    Both fields are optional: a session with no cloud server
    configured has neither, and the tools that need one check it
    rather than building a request against an empty string (DEC-14).
    """

    token: str | None = None
    server_url: str | None = None


@dataclass(frozen=True)
class ToolRegistryDeps:
    """What the built-in tool registry needs to build the core tools.

    The registry is the only consumer of every field here, which is
    why they travel together: the builder hands the whole bag to
    :class:`~igni_core.core.tools.registry.ToolRegistry` and the
    permission class in one go.
    """

    project_dir: Path
    settings: Settings
    permissions_cls: type[ToolPermissions]
    registry_cls: type[ToolRegistry]
    cloud: CloudCredentials
    broadcast: BroadcastSink
    #: The supervisor the shell toolkit registers its processes on —
    #: the one the backend process shares.
    supervisor: ProcessSupervisor


@dataclass(frozen=True)
class ToolSourceDeps:
    """Where the custom toolkits come from.

    * ``plugin_loader`` — discovers the tool directories plugins ship.
    * ``disabled_plugins`` — the plugins the session has switched off.
    * ``group_tools_dir`` — the organisation's own tools directory.
    """

    plugin_loader: PluginLoader
    disabled_plugins: set[str]
    group_tools_dir: Path | None = None


@dataclass(frozen=True)
class OrchestrationDeps:
    """The delegation surface: what spawning a sub-agent needs."""

    pool: AgentPool
    hook_executor: HookExecutor
    session_id: str
    spawn_budget: SpawnBudget
    sub_agent_hitl: SubAgentHITLCoordinator


@dataclass(frozen=True)
class SessionToolkits:
    """The toolkits that answer back into a live session.

    ``LoopTools`` / ``LoopProgressTool`` / ``TodoTools`` /
    ``PlanTool`` / ``SlashCommandTool`` reach into the session's
    stores and broadcast paths, so they cannot be built from the
    dependency bags alone. The coordinator — which owns the session —
    builds them with :meth:`for_session` and hands them over
    finished; the builder never sees the session.
    """

    loop: LoopTools
    loop_progress: LoopProgressTool
    todo: TodoTools
    plan: PlanTool
    slash: SlashCommandTool

    @classmethod
    def for_session(cls, session: Session) -> SessionToolkits:
        """Build the session-facing toolkits for ``session``."""
        return cls(
            loop=LoopTools(session),
            loop_progress=LoopProgressTool(session),
            todo=TodoTools(session),
            plan=PlanTool(session),
            slash=SlashCommandTool(session),
        )


@dataclass(frozen=True)
class ToolBuildDeps:
    """Everything the tools builder needs, in one typed bag.

    The builder is session-free: the session-facing toolkits arrive
    already built in ``session_toolkits``, and every other
    collaborator arrives as a collaborator rather than as an
    attribute copied off the session.
    """

    registry: ToolRegistryDeps
    sources: ToolSourceDeps
    orchestration: OrchestrationDeps
    mcp_manager: MCPClientManager
    reasoning_factory: Callable[[Settings], Toolkit | None]
    resolve_tool_names: Callable[[ToolRegistry], list[str]]
    session_toolkits: SessionToolkits
    lsp_manager: LspServerManager | None = None
    monitor_manager: MonitorManager | None = None


__all__ = [
    "CloudCredentials",
    "OrchestrationDeps",
    "SessionToolkits",
    "ToolBuildDeps",
    "ToolRegistryDeps",
    "ToolSourceDeps",
]
