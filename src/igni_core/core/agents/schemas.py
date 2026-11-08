"""Pydantic models + typed protocols for the agents package.

* :class:`AgentPriority` — resolution priorities as an :class:`IntEnum`.
* :class:`AgentDefinition` — parsed frontmatter + body from a
  ``.md`` file.
* :class:`AgentInfo` — wire format for the agents panel.
* :class:`AgentEntry` — a definition plus its resolution priority.
* :class:`LoadReport` + :class:`LoadError` — what a directory-load
  pass accepted and which files it could not parse.
* :class:`DbHandle` / :class:`McpClient` — structural protocols for
  the Agno DB and MCP toolkit, so this module does not import Agno.
* :class:`AgentConstructorArgs` — the typed bundle handed to Agno's
  ``Agent`` through :meth:`AgentConstructorArgs.to_agno_kwargs`.
* :class:`AgentBuildContext` — the shared state (settings, base dir,
  MCP clients, DB, broadcast) :class:`AgentBuilder` builds against.
"""

from __future__ import annotations

from enum import IntEnum
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from igni_core.core.agents.trained_layout import TrainedLayout
from igni_core.core.agno_telemetry import AgnoTelemetry
from igni_core.core.broadcast_schema import BroadcastSink
from igni_core.core.config.settings import Settings


class AgentPriority(IntEnum):
    """Resolution priorities — highest wins on same-name collision.

    Within the same scope, native Ember sources beat cross-tool
    Claude sources by +1::

        10  ephemeral agents created at runtime
         6  an extension's <agent>.<variant>.md, while it runs
         5  <project>/.igni/agents/          (project, native)
         4  <project>/.igni/agents.local/    (project personal)
         3  <project>/.claude/agents/         (project, cross-tool)
         2  <group policy cache>/agents/      (the org's group)
         1  ~/.igni/agents/                  (user, native)
         0  ~/.claude/agents/                 (user, cross-tool)

    The org's row is above the user's globals and below everything the
    project declares. It used to sit at the top; the project now
    outranks it, so a repository that ships its own version of an agent
    gets that version.

    An extension's override sits above all of them: the agents it
    replaces are written for the capability it adds, and while it runs
    the plain version would be told about tools and an index that it
    does not use. It applies only while the extension is live.
    """

    USER_CLAUDE = 0
    USER_EMBER = 1
    ORG_GROUP = 2
    PROJECT_CLAUDE = 3
    PROJECT_LOCAL = 4
    PROJECT_EMBER = 5
    EXTENSION_OVERRIDE = 6
    EPHEMERAL = 10


class AgentDefinition(BaseModel):
    """Parsed agent definition from a .md file."""

    name: str
    description: str
    tools: list[str] = Field(default_factory=list)
    model: str | None = None
    color: str | None = None
    reasoning: bool = False
    reasoning_min_steps: int = 1
    reasoning_max_steps: int = 10
    tags: list[str] = Field(default_factory=list)
    can_orchestrate: bool = True
    mcp_servers: list[str] = Field(default_factory=list)
    max_turns: int | None = None
    temperature: float | None = None
    # Per-agent output token cap. ``None`` = use provider default.
    # Overriding rescues agents whose whole reply IS the payload
    # (e.g. visualizer) from mid-stream truncation.
    max_tokens: int | None = None
    system_prompt: str = ""
    source_path: Path | None = None
    # ``"worktree"`` on plugin-shipped agents so their spawns run
    # in a fresh worktree; ``None`` for user / project agents.
    force_isolation: Literal["worktree"] | None = None

    def namespaced(self, prefix: str) -> AgentDefinition:
        """Return a copy with ``name`` prefixed by ``<prefix>:``.

        Used by the plugin loader so each plugin's agents land
        under their own namespace and can't collide with same-
        named agents from other plugins or the user's own
        ``.igni/agents/``.
        """
        return self.model_copy(update={"name": f"{prefix}:{self.name}"})


class AgentInfo(BaseModel):
    """Wire format for one agent — emitted by
    :meth:`BackendServer.get_agent_details`, consumed by the
    agents panel.

    Sub-set of :class:`AgentDefinition` adapted for JSON transport:
    ``source_path`` is widened to ``str`` (Path doesn't serialize),
    and ``is_ephemeral`` is computed at the backend.
    """

    name: str
    description: str = ""
    tools: list[str] = Field(default_factory=list)
    model: str = ""
    color: str = ""
    can_orchestrate: bool = True
    mcp_servers: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    system_prompt: str = ""
    source_path: str = ""
    is_ephemeral: bool = False
    #: Tool names igni cannot resolve. Empty for almost every agent;
    #: when it is not, this one raises the moment something calls it.
    unknown_tools: list[str] = Field(default_factory=list)


class AgentEntry(BaseModel):
    """One entry in the :class:`AgentPool` — a definition plus its
    resolution priority.

    Typed pair-container per Rule 1.
    """

    model_config = ConfigDict(frozen=False)

    definition: AgentDefinition
    priority: AgentPriority


class LoadError(BaseModel):
    """One parse failure from a directory-load pass.

    Kept as a typed record (not a free-form string) so the caller
    can act on the source path without regex-scraping a message.
    """

    path: Path
    reason: str


class LoadReport(BaseModel):
    """Result of :meth:`AgentDefinitionLoader.load` — accepted
    entries plus per-file parse errors.

    Replaces the previous ``print(..., file=sys.stderr)`` swallow —
    callers can now surface errors to the FE, decide whether to
    fail the boot, or route them to the audit log.
    """

    entries: dict[str, AgentEntry] = Field(default_factory=dict)
    errors: list[LoadError] = Field(default_factory=list)

    def merge(self, other: LoadReport) -> None:
        """Merge ``other`` into this report, honouring priority for
        collisions on ``entries`` and appending errors."""
        for name, entry in other.entries.items():
            existing = self.entries.get(name)
            if existing is None or entry.priority > existing.priority:
                self.entries[name] = entry
        self.errors.extend(other.errors)


# ── Protocols for external collaborators ─────────────────────────
#
# The real classes (Agno's ``AsyncBaseDb`` and ``MCPTools``) pull in
# Agno and its storage bindings; a bare ``AgentPool()`` in a test
# shouldn't pay that. Each protocol names the members the real class
# has, so a db that is not one fails the field check. The pool fills
# ``mcp_clients`` through ``model_copy``, which skips validation, so
# there the protocol is a static type only.


@runtime_checkable
class DbHandle(Protocol):
    """Agno ``AsyncBaseDb`` handle. Threaded through so paused
    sub-agent runs land in the same store as the parent."""

    async def get_session(self, *args: Any, **kwargs: Any) -> Any: ...

    async def upsert_session(self, *args: Any, **kwargs: Any) -> Any: ...

    async def close(self) -> None: ...


@runtime_checkable
class McpClient(Protocol):
    """One connected Agno ``MCPTools`` — placed in the ``tools=[...]``
    list of an agent to expose its remote tools locally."""

    initialized: bool

    async def connect(self, *args: Any, **kwargs: Any) -> None: ...

    async def close(self) -> None: ...


class AgentConstructorArgs(BaseModel):
    """Typed bundle of the kwargs :class:`AgentBuilder` hands to
    :class:`agno.agent.Agent`.

    Replaces the ``kwargs: dict[str, Any]`` splat that used to
    hand-mutate an Agent construction dict (Rule 1). Nothing here
    knows about Agno's ``**kwargs`` — the builder converts through
    :meth:`to_agno_kwargs` at the boundary.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    model: Any
    description: str
    instructions: list[str] | None
    tools: list[Any] | None
    markdown: bool = True
    num_history_runs: int
    retries: int
    db: Any | None = None
    reasoning: bool = False
    reasoning_min_steps: int | None = None
    reasoning_max_steps: int | None = None
    tool_hooks: list[Any] | None = None

    def to_agno_kwargs(self) -> dict[str, Any]:
        """Emit the kwargs Agno actually expects.

        Drops the reasoning trio when ``reasoning=False`` and the
        ``db`` key when ``db is None`` — Agno differentiates
        between "missing key" (use its default) and "key with
        None" (explicit disable) for both.
        """
        # Rendered as igni-1.0 was trained to read them (``trained_layout``).
        instructions, markdown = TrainedLayout.instructions(
            self.instructions, markdown=self.markdown
        )
        kwargs: dict[str, Any] = {
            "name": self.name,
            "model": self.model,
            "description": self.description,
            "instructions": instructions,
            "tools": self.tools,
            "markdown": markdown,
            "num_history_runs": self.num_history_runs,
            "retries": self.retries,
            # Agno's run telemetry to os-api.agno.com (core/agno_telemetry.py).
            "telemetry": AgnoTelemetry.CONSTRUCTOR_VALUE,
        }
        if self.db is not None:
            kwargs["db"] = self.db
        if self.tool_hooks:
            kwargs["tool_hooks"] = self.tool_hooks
        if self.reasoning:
            kwargs["reasoning"] = True
            kwargs["reasoning_min_steps"] = self.reasoning_min_steps
            kwargs["reasoning_max_steps"] = self.reasoning_max_steps
        return kwargs


class AgentBuildContext(BaseModel):
    """Shared state :class:`AgentBuilder` needs to construct any
    agent from a definition.

    :class:`AgentPool` re-constructs only the fields that change
    (e.g. ``mcp_clients`` after MCP connects) via
    ``context.model_copy(update={...})`` instead of touching
    private builder state. ``settings`` is ``None`` only until the
    pool's ``load_definitions`` runs.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    settings: Settings | None = None
    base_dir: str | None = None
    mcp_clients: dict[str, McpClient] | None = None
    db: DbHandle | None = None
    broadcast: BroadcastSink | None = None


__all__ = [
    "AgentBuildContext",
    "AgentConstructorArgs",
    "AgentDefinition",
    "AgentEntry",
    "AgentInfo",
    "AgentPriority",
    "DbHandle",
    "LoadError",
    "LoadReport",
    "McpClient",
]
