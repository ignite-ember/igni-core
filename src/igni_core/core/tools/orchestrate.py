"""OrchestrateTools — the toolkit that lets an agent delegate.

The class is a thin Agno-facing facade: three tool methods
(``spawn_agent`` / ``spawn_team`` / ``create_agent``) that build the
right spawn runner and delegate to it.

What it holds is two bundles rather than a dozen attributes:

* :class:`OrchestrateScope` — the pool, the settings, the budget, the
  depths.
* :class:`SpawnWiring` — the session id, the hook executor and the
  sinks the runs under it report through.

Both are public, so the session can wire a sink on after the toolkit
is built (``backend/team_wiring.py``) and every copy of the toolkit
Agno makes for a run sees it: the bundles are shared, not copied.

Sibling modules own the rest:

* :class:`SpawnBudget` (``orchestrate_budget.py``) — per-session
  sub-agent counter, owned by the session and handed to every
  toolkit it builds (nested toolkits get their parent's).
* :class:`SpawnSandbox` (``orchestrate_sandbox.py``) — one
  per-spawn worktree lifecycle. Failure mode is a typed
  :class:`SandboxSetupResult` envelope.
* :class:`SpawnRunner` (``orchestrate_spawn_runner.py``) plus
  :class:`AgentSpawn` (``orchestrate_spawn_agent.py``) and
  :class:`TeamSpawn` (``orchestrate_spawn_team.py``) — the template
  every spawn follows and its two shapes.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from agno.team.team import Team
from agno.tools import Toolkit

from igni_core.core.config.models import ModelRegistry
from igni_core.core.tools.orchestrate_budget import SpawnBudget
from igni_core.core.tools.orchestrate_contracts import HitlCoordinatorProtocol
from igni_core.core.tools.orchestrate_spawn_agent import AgentSpawn
from igni_core.core.tools.orchestrate_spawn_team import TeamSpawn
from igni_core.core.tools.orchestrate_spawn_types import (
    NestedToolkitSpec,
    OrchestrateScope,
    SpawnContext,
    SpawnDeps,
    SpawnWiring,
    TeamSpec,
)
from igni_core.core.tools.orchestrate_stream_context import StreamSinks
from igni_core.core.tools.orchestrate_streaming import (
    run_agent_streaming,
    run_team_streaming,
)
from igni_core.core.tools.spawn_context import ParentCall, SpawnCalls

__all__ = ["OrchestrateTools"]

if TYPE_CHECKING:
    from igni_core.core.agents import AgentPool
    from igni_core.core.config.settings import Settings
    from igni_core.core.hooks.executor import HookExecutor


def _build_agno_team(spec: TeamSpec) -> Any:
    """The Agno ``Team`` a :class:`TeamSpec` describes.

    The only place an Agno team is constructed: everything above it
    speaks in specs, so the construction is one typed decision rather
    than keyword arguments spread over the call sites.
    """
    return Team(
        name=spec.name,
        mode=spec.mode.value,
        model=spec.model,
        members=spec.members,
        markdown=spec.markdown,
        telemetry=spec.telemetry,
        db=spec.db,
        max_iterations=spec.max_iterations,
    )


def _model_registry(settings: Settings) -> ModelRegistry:
    """The registry a sub-team's model comes from."""
    return ModelRegistry(settings)


def _build_nested_toolkit(spec: NestedToolkitSpec) -> OrchestrateTools:
    """The orchestrate toolkit one level down.

    Built by the same facade that built the parent's toolkit, so the
    nested one shares the parent's scope — one session-wide budget,
    one pool — and the parent's wiring, which is where the sinks live.
    Sharing the wiring rather than copying it is what lets a progress
    callback wired on after the session started reach a nested run.
    """
    scope = spec.scope
    wiring = spec.wiring
    nested = OrchestrateTools(
        pool=scope.pool,
        settings=scope.settings,
        current_depth=spec.depth,
        hook_executor=wiring.hook_executor,
        session_id=wiring.session_id,
        hitl_coordinator=wiring.sinks.hitl_coordinator,
        project_dir=scope.project_dir,
        budget=scope.budget,
    )
    nested.wiring = wiring
    return nested


class OrchestrateTools(Toolkit):
    """Tools for agents to spawn sub-agents from the agent pool.

    Agno-facing facade — every tool method builds a
    :class:`SpawnRunner` and delegates. What the runner needs arrives
    as two bundles, :attr:`scope` and :attr:`wiring`, which every
    copy of this toolkit shares: a late-arriving wire (e.g.
    :meth:`TeamWiring.wire_orchestrate_progress` setting the progress
    sink on the original) is picked up by every future per-run copy.
    """

    def __init__(
        self,
        pool: AgentPool,
        settings: Settings,
        current_depth: int = 0,
        hook_executor: HookExecutor | None = None,
        session_id: str = "",
        hitl_coordinator: HitlCoordinatorProtocol | None = None,
        project_dir: Path | None = None,
        budget: SpawnBudget | None = None,
    ):
        super().__init__(name="ember_orchestrate")
        self.scope = OrchestrateScope(
            pool=pool,
            settings=settings,
            # The session's sub-agent cap, shared by every toolkit of
            # the session at every depth. A toolkit built on its own
            # gets a fresh one at the configured cap.
            budget=budget
            if budget is not None
            else SpawnBudget(settings.orchestration.max_total_agents),
            current_depth=current_depth,
            max_depth=settings.orchestration.max_nesting_depth,
            # Required for ``isolation="worktree"`` spawns — the
            # worktree is forked from this repo. ``None`` disables the
            # isolation feature (spawn_agent returns an error if the
            # agent requests it without a project_dir wired in).
            project_dir=project_dir,
        )
        self.wiring = SpawnWiring(
            session_id=session_id,
            hook_executor=hook_executor,
            sinks=StreamSinks(hitl_coordinator=hitl_coordinator),
        )
        self.register(self.spawn_agent)
        self.register(self.spawn_team)
        if settings.orchestration.generate_ephemeral:
            self.register(self.create_agent)

    # ── What the scope holds ──────────────────────────────────────

    @property
    def pool(self) -> AgentPool:
        """The agents a spawn may draw from."""
        return self.scope.pool

    @property
    def settings(self) -> Settings:
        """The session's settings — model, orchestration limits, timeouts."""
        return self.scope.settings

    @property
    def current_depth(self) -> int:
        """How deep this toolkit sits. Zero is the top level."""
        return self.scope.current_depth

    @property
    def max_depth(self) -> int:
        """How deep a spawn under this toolkit may still nest."""
        return self.scope.max_depth

    # ── Dependency wiring ─────────────────────────────────────────

    def spawn_deps(self) -> SpawnDeps:
        """The factories spawns are built with.

        Read from this module's globals at call time — not captured
        when the toolkit was built — so a test that patches
        ``igni_core.core.tools.orchestrate.run_agent_streaming`` /
        ``.Team`` / ``.ModelRegistry`` sees its patch take effect on
        every spawn.
        """
        return SpawnDeps(
            agent_streamer=run_agent_streaming,
            team_streamer=run_team_streaming,
            team_factory=_build_agno_team,
            model_registry_factory=_model_registry,
            nested_toolkit_factory=_build_nested_toolkit,
        )

    def _spawn_context(self, task: str) -> SpawnContext:
        """What one spawn needs from this toolkit."""
        return SpawnContext(
            scope=self.scope,
            wiring=self.wiring,
            deps=self.spawn_deps(),
            task=task,
        )

    # ── Tool methods (Agno-facing) ────────────────────────────────

    async def spawn_agent(
        self,
        task: str,
        agent_name: str,
        isolation: str = "",
        agent: Any = None,
        run_context: Any = None,
    ) -> str:
        """Run a single agent from the pool on a subtask.

        Args:
            task: The subtask description for the agent.
            agent_name: Name of the agent to spawn (from the pool).
            isolation: Optional isolation mode. Currently the only
                non-empty value is ``"worktree"`` — creates a
                fresh git worktree branched off the session's
                project, runs the agent with its file/shell tools
                rebased to that worktree, then either cleans up
                (no changes) or preserves the worktree (changes
                remain on the new branch for the caller to merge
                or discard). Tools without a ``base_dir``
                attribute (most MCP clients) still see the
                original project dir.

        Returns:
            The agent's response with activity log. When the
            spawn was isolated, a ``Worktree:`` footer reports
            the branch + path so the caller knows where the
            changes landed.

        ``agent`` and ``run_context`` are Agno's to fill (it leaves them
        out of the tool's schema): the run waiting for this call, which
        lets an approval the spawned agent asks for outlive the backend
        (:mod:`igni_core.core.tools.spawn_context`).
        """
        parent = ParentCall.from_run_context(
            run_context,
            agent=agent,
            tool_name="spawn_agent",
            tool_args={"task": task, "agent_name": agent_name, "isolation": isolation},
        )
        with SpawnCalls.entered(parent):
            return await self._spawn_one(task, agent_name, isolation)

    async def _spawn_one(self, task: str, agent_name: str, isolation: str = "") -> str:
        runner = AgentSpawn(
            context=self._spawn_context(task),
            agent_name=agent_name,
            isolation=isolation,
        )
        return await runner.run()

    async def spawn_team(
        self,
        task: str,
        agent_names: str,
        mode: str = "coordinate",
        agent: Any = None,
        run_context: Any = None,
    ) -> str:
        """Create and run a sub-team for a specific subtask.

        Args:
            task: The subtask description.
            agent_names: Comma-separated agent names from the pool.
            mode: Team mode: "coordinate", "route", "broadcast", or "tasks".

        Returns:
            The team's response with activity log.
        """
        names = [n.strip() for n in agent_names.split(",") if n.strip()]
        # Single-name teams collapse to a plain spawn — preserved
        # behaviour from the pre-refactor code.
        parent = ParentCall.from_run_context(
            run_context,
            agent=agent,
            tool_name="spawn_team",
            tool_args={"task": task, "agent_names": agent_names, "mode": mode},
        )
        with SpawnCalls.entered(parent):
            if len(names) == 1:
                return await self._spawn_one(task, names[0])
            return await self._spawn_team(task, names, mode)

    async def _spawn_team(self, task: str, names: list[str], mode: str) -> str:
        runner = TeamSpawn(
            context=self._spawn_context(task),
            names=names,
            mode=mode,
        )
        return await runner.run()

    def create_agent(
        self,
        name: str,
        description: str,
        system_prompt: str,
        tools: str = "Write,Edit,Bash",
    ) -> str:
        """Create a new ephemeral agent with a custom system prompt.

        Args:
            name: Short snake_case name for the agent.
            description: One-line description of what the agent does.
            system_prompt: Full system prompt defining the agent's behavior.
            tools: Comma-separated tool names (e.g. "Bash,Edit,Write").
                Valid: Bash, BashOutput, Edit, NotebookEdit, Schedule,
                WebFetch, WebSearch, Write. Searching and reading files
                go through Bash (``rg``, ``cat``, ``find``) — there are no
                separate Read/Grep/Glob/LS toolkits.

        Returns:
            Confirmation message with the agent name.
        """
        # ``tools: str`` (CSV) is an intentional exception to the
        # audit's "list[str] over CSV string" preference: Agno's
        # tool-argument marshalling emits strings for tool arguments,
        # not lists, so a ``list[str]`` here breaks the tool contract.
        tool_list = [t.strip() for t in tools.split(",") if t.strip()]
        try:
            self.pool.ephemeral.register(
                name=name,
                description=description,
                system_prompt=system_prompt,
                tools=tool_list,
            )
            return (
                f"Created ephemeral agent '{name}': {description}. "
                f"Use spawn_agent(task, '{name}') to delegate."
            )
        except (ValueError, RuntimeError) as e:
            return f"Error creating agent: {e}"
