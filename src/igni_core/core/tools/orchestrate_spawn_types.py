"""The values a spawn is built out of.

A spawn — one sub-agent, or one sub-team — needs three kinds of
thing:

* the **scope** it runs in: the agent pool it draws from, the
  settings it obeys, the budget it draws on, how deep it sits.
  :class:`OrchestrateScope` is that bundle.
* the **wiring** it reports through: the session id, the hook
  executor, and the four sinks the run talks to.
  :class:`SpawnWiring` is that bundle.
* the **dependencies** it builds with: the factories that make an
  Agno team, the registry that resolves a model, the toolkit a
  nested spawn is given, the two streaming coroutines.
  :class:`SpawnDeps` is that bundle.

:class:`SpawnContext` puts the three together with the task, so a
spawn runner holds one reference instead of a dozen fields.

Around them sit the envelopes a spawn answers with — what it built
(:class:`AgentBuildResult`, :class:`SubTeamBuildResult`), what it
told the hooks (:class:`SubagentStartPayload`,
:class:`SubagentStopPayload`), and how it failed
(:class:`SpawnResult`) — and the two modes it understands
(:class:`TeamMode`, :class:`IsolationMode`).

Every one of them is a model, so a spawn's boundary is typed rather
than a bag of ``Any``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SkipValidation

from igni_core.core.agents.pool import AgentPool
from igni_core.core.agents.schemas import AgentDefinition
from igni_core.core.config.settings import Settings
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.tools.orchestrate_budget import SpawnBudget
from igni_core.core.tools.orchestrate_modes import IsolationMode, TeamMode
from igni_core.core.tools.orchestrate_sandbox import SpawnSandbox
from igni_core.core.tools.orchestrate_stream_context import StreamContext, StreamSinks


class SpawnWiring(BaseModel):
    """The collaborators a spawn reports through.

    They are all late-bound: the session wires them onto the toolkit
    after it is built, so a spawn reads them at the moment it runs
    rather than at the moment it was constructed.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    #: The session this spawn belongs to.
    session_id: str = ""
    #: Where the spawn's hooks go, when the session has any.
    hook_executor: SkipValidation[HookExecutor | None] = None
    #: The four sinks the spawn and the runs under it report through.
    sinks: StreamSinks = Field(default_factory=StreamSinks)


class OrchestrateScope(BaseModel):
    """What a toolkit — and every spawn under it — is built around.

    The agent pool it draws from, the settings it obeys, the budget
    it draws on and the depth it sits at. One bundle, so a toolkit
    holds one reference instead of six, and so a nested toolkit is
    built from its parent's scope rather than from a copy of six
    arguments.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    #: The agents a spawn may draw from.
    pool: SkipValidation[AgentPool]
    #: The session's settings — model, orchestration limits, timeouts.
    settings: SkipValidation[Settings]
    #: The session-wide cap on how many sub-agents may run.
    budget: SkipValidation[SpawnBudget]
    #: How deep this toolkit sits. Zero is the top level.
    current_depth: int = 0
    #: How deep a spawn under this toolkit may still nest.
    max_depth: int = 5
    #: The project a spawn's worktree is forked from, when it has one.
    project_dir: Path | None = None


class NestedToolkitSpec(BaseModel):
    """What a nested spawn's toolkit is built from.

    The nested toolkit is the orchestrate toolkit one level down. It
    is built by the same facade that built this spawn's toolkit, so
    the factory only needs to be told which depth it is for, the
    scope it shares, and the wiring the nested run reports through.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    #: The depth the nested toolkit runs at — one below its parent.
    depth: int
    #: The scope the nested toolkit shares with its parent.
    scope: OrchestrateScope
    #: The wiring the nested run shares with its parent.
    wiring: SpawnWiring


#: Agno's own default for a team's task-iteration loop. Only the
#: ``tasks`` mode has such a loop; in the other modes the cap is
#: never read, and leaving it at Agno's default keeps it out of the
#: team's stored configuration.
DEFAULT_TEAM_MAX_ITERATIONS = 10


class TeamSpec(BaseModel):
    """The team a sub-team spawn runs.

    Everything the Agno ``Team`` is constructed with, gathered here
    so the construction is one typed decision rather than a bag of
    keyword arguments spread over the call site.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    #: The team's name.
    name: str
    #: The mode the team coordinates its members in.
    mode: TeamMode
    #: The members, each a per-spawn copy of a pooled agent.
    members: list[Any] = Field(default_factory=list)
    #: The model the team leader runs on.
    model: Any = None
    #: The store the team's run is kept in — the session's own, so a
    #: paused run can be found again.
    db: Any = None
    #: Whether the team's own answer is rendered as markdown.
    markdown: bool = True
    #: Whether Agno reports this team's runs to its telemetry.
    telemetry: bool = False
    #: How many task iterations the team may take — the ``tasks``
    #: mode's own loop. Only the ``tasks`` mode has one.
    max_iterations: int = DEFAULT_TEAM_MAX_ITERATIONS


class TeamBuildRequest(BaseModel):
    """What a sub-team is built from.

    The team a ``spawn_team`` call runs, and the team a restart cut
    off is rebuilt as (``backend/cold_spawn_resume.py``), are the same
    team: both go through the same builder, so both name their
    inputs the same way.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    #: The pool the members are drawn from.
    pool: SkipValidation[AgentPool]
    #: The session's settings — the team's model and its task cap.
    settings: SkipValidation[Settings]
    #: The member names, in the order the caller gave them.
    names: list[str] = Field(default_factory=list)
    #: The mode the caller asked for.
    mode: str = ""
    #: The depth the team runs at.
    depth: int = 1


#: The two streaming coroutines a spawn delegates to. Both answer
#: ``(response, activity log)``.
Streamer = Callable[[StreamContext], Awaitable[tuple[str, list[str]]]]

#: The factory that builds an Agno ``Team`` from a :class:`TeamSpec`.
TeamFactory = Callable[[TeamSpec], Any]

#: The factory that builds the registry a sub-team's model comes from.
ModelRegistryFactory = Callable[[Settings], Any]

#: The factory that builds the toolkit a nested spawn is given.
NestedToolkitFactory = Callable[[NestedToolkitSpec], Any]


class SpawnDeps(BaseModel):
    """The factories a spawn builds its runnable with.

    They are injected rather than imported so the spawn runner does
    not have to know how an Agno team is put together, and so a test
    can hand a spawn a recording stand-in instead of a real team.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True, frozen=True)

    #: Streams one specialist agent.
    agent_streamer: Streamer
    #: Streams one coordinated team.
    team_streamer: Streamer
    #: Builds the Agno team a sub-team spawn runs.
    team_factory: TeamFactory
    #: Builds the registry a sub-team's model comes from.
    model_registry_factory: ModelRegistryFactory
    #: Builds the toolkit a nested spawn is given.
    nested_toolkit_factory: NestedToolkitFactory


class SpawnContext(BaseModel):
    """Everything a spawn needs from the session it runs in.

    The collaborators arrive as three bundles rather than as thirteen
    arguments, so a spawn runner holds one reference instead of
    thirteen fields, and adding a collaborator is one field on one
    bundle rather than a parameter on the base class and on both of
    its subclasses.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    #: The pool, settings, budget and depths the spawn runs under.
    scope: OrchestrateScope
    #: Who the spawn reports to.
    wiring: SpawnWiring
    #: What the spawn builds its runnable with.
    deps: SpawnDeps
    #: The task the spawn was asked to do.
    task: str = ""


class SpawnResult(BaseModel):
    """What a failed spawn says.

    The spawn's outer return is a string — Agno's tool-return
    contract — but the boundary between the failing branch and the
    place that renders the error is typed through this envelope, so
    the failure keeps its information instead of being flattened into
    a message at the point it is caught.
    """

    model_config = ConfigDict(populate_by_name=True)

    ok: bool = False
    message: str = ""

    @classmethod
    def failure(cls, message: str) -> SpawnResult:
        """A spawn that did not succeed, and why."""
        return cls(ok=False, message=message)

    @classmethod
    def success(cls) -> SpawnResult:
        """A spawn that did."""
        return cls(ok=True, message="")


class SubagentStartPayload(BaseModel):
    """Wire payload for the ``SubagentStart`` hook event.

    Fired at the top of every ``spawn_agent`` / ``spawn_team``, right
    after the budget reservation. Consumers (audit log, telemetry)
    see the agent name and the truncated task; ``mode`` is populated
    for team spawns and stays ``None`` for single-agent spawns.
    """

    model_config = ConfigDict(populate_by_name=True)

    session_id: str
    agent_name: str
    task: str
    mode: str | None = None


class SubagentStopPayload(BaseModel):
    """Wire payload for the ``SubagentStop`` hook event.

    Fired in every exit path of a spawn — success, timeout, and the
    generic ``except``. Exactly one of ``result_preview`` / ``error``
    is populated per event; the other stays ``None``.
    """

    model_config = ConfigDict(populate_by_name=True)

    session_id: str
    agent_name: str
    result_preview: str | None = None
    error: str | None = None


@dataclass
class AgentBuildResult:
    """What a single-agent spawn built.

    An unknown agent name and a worktree that could not be created
    both land as ``AgentBuildResult(agent=None, error='...')``. On
    success it carries the per-spawn copy of the agent, the
    definition it came from, the effective isolation mode (which may
    have been forced to ``"worktree"`` by the definition), the
    sandbox that was set up, and the task the sandbox wants the agent
    to work on.

    It is a dataclass rather than a model because the agent it carries
    is an Agno ``Agent`` — a dataclass itself, which pydantic cannot
    build a schema for.
    """

    agent: Any = None
    defn: AgentDefinition | None = None
    isolation: IsolationMode = IsolationMode.NONE
    sandbox: SpawnSandbox | None = None
    task: str = ""
    error: str | None = None


@dataclass
class SubTeamBuildResult:
    """What a sub-team spawn built.

    An unknown member name lands as
    ``SubTeamBuildResult(team=None, mode=None, error='...')``; on
    success it carries the built team and the mode it was built in.

    A dataclass rather than a model, for the same reason as
    :class:`AgentBuildResult`: the team is an Agno ``Team``.
    """

    team: Any = None
    mode: TeamMode | None = None
    error: str | None = None


class AgentSpawnState(BaseModel):
    """What one single-agent spawn knows about itself."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    #: The pooled agent this spawn runs.
    agent_name: str
    #: The isolation the caller asked for, before it is parsed.
    isolation_raw: str = ""
    #: The isolation in force.
    isolation: IsolationMode = IsolationMode.NONE
    #: The per-spawn copy of the agent, once it is built.
    agent: Any = None
    #: The agent definition's description, for the response header.
    description: str = ""
    #: The agent definition's tools, for the response header.
    tools: str = "none"
    #: The task the sandbox wants the agent to work on.
    effective_task: str = ""
    #: The sandbox the spawn runs in, when it has one.
    sandbox: SkipValidation[SpawnSandbox | None] = None


class TeamSpawnState(BaseModel):
    """What one sub-team spawn knows about itself."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    #: The pooled agents the team is made of.
    names: list[str] = Field(default_factory=list)
    #: The mode the caller asked for, before it is parsed.
    mode_raw: str = ""
    #: The mode in force.
    mode: TeamMode = TeamMode.COORDINATE
    #: The built team, once it is built.
    team: Any = None
    #: One line per member, for the response header.
    member_lines: list[str] = Field(default_factory=list)
    #: The label the team is known by in progress events.
    team_label: str = ""


__all__ = [
    "AgentBuildResult",
    "AgentSpawnState",
    "DEFAULT_TEAM_MAX_ITERATIONS",
    "IsolationMode",
    "ModelRegistryFactory",
    "NestedToolkitFactory",
    "NestedToolkitSpec",
    "OrchestrateScope",
    "SpawnBudget",
    "SpawnContext",
    "SpawnDeps",
    "SpawnResult",
    "SpawnWiring",
    "Streamer",
    "SubTeamBuildResult",
    "SubagentStartPayload",
    "SubagentStopPayload",
    "TeamBuildRequest",
    "TeamFactory",
    "TeamMode",
    "TeamSpec",
    "TeamSpawnState",
]
