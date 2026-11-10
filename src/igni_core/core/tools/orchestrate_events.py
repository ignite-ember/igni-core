"""Pydantic wire models + protocols for :mod:`orchestrate_streaming`.

This file promotes the ~14 raw-dict ``on_progress`` payload shapes
that used to live inline in the two streaming generators to typed
models (including :class:`VisualizationDeltaEvent` for the visualizer
sub-agent's progressive tool-call arg stream), adds the collaborator
protocols (``EventAppender``, ``HitlCoordinatorProtocol``,
``AgnoRunnable``, ``OnProgress``), and centralises the FE-facing
icon set behind :class:`LogSymbols`.

Rationale (per the refactor audit):

* Rule 3 (no inline data blobs): tree-drawing / status characters
  (⏸ ✓ ✗ … ⚠ ├─ │ ┌─ ╞═ └─) were sprinkled through the streaming
  code as bare string literals. They now sit on :class:`LogSymbols`;
  call sites reference the named symbol.
* Untyped ``Any`` for the callback surface: ``on_progress``,
  ``hitl_coordinator``, ``agent`` / ``team`` are typed via structural
  protocols so subclasses of :class:`BaseStreamHandler` state their
  dependencies clearly.
* Class-level singleton ``OrchestrateTools._active_subagent_runs``
  is replaced by :class:`SubAgentRegistry` — an ordinary instance
  collaborator that :class:`OrchestrateTools` constructs once and
  passes into every handler.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

import jiter
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    pass


# ── Event payload models ────────────────────────────────────────────


class _EventBase(BaseModel):
    """Common fields of every sub-agent progress event.

    Every event names the agent that emitted it (``agent_path``) and
    carries a fixed ``type`` discriminator. ``card_id`` groups all
    events of one spawn so a client can show them on the same
    team-progress card across page refreshes and concurrent spawns.
    """

    model_config = ConfigDict(populate_by_name=True)

    type: str
    agent_path: str = Field(
        description="Dot-joined names of the agent that emitted the event, from the outermost "
        "spawning agent down to the emitter (`root` for the top level)."
    )
    card_id: str | None = Field(
        default=None,
        description="Identifier of the team-progress card this event belongs to. All events "
        "of one spawn share it. Null when the event is not tied to a card.",
    )


class ToolStartedEvent(_EventBase):
    """A sub-agent started calling a tool."""

    type: Literal["tool_started"] = Field(default="tool_started", frozen=True)
    tool: str = Field(description="Name of the tool being called.")
    tool_call_id: str | None = Field(
        default=None,
        description="Identifier of this tool call; matches the `tool_completed` event for "
        "the same call. Null when the model did not supply one.",
    )
    args: str = Field(
        default="",
        description="Preview of the tool arguments as text (usually JSON, may be truncated). Empty when none.",
    )


class ToolCompletedEvent(_EventBase):
    """A sub-agent's tool call finished."""

    type: Literal["tool_completed"] = Field(default="tool_completed", frozen=True)
    tool: str = Field(description="Name of the tool that was called.")
    tool_call_id: str | None = Field(
        default=None,
        description="Identifier of the tool call; matches the earlier `tool_started` event. "
        "Null when the model did not supply one.",
    )
    result: str = Field(
        default="",
        description="Preview of the tool output, or the error message when `is_error` is true; may be truncated. Empty when none.",
    )
    is_error: bool = Field(default=False, description="True when the tool call failed.")


class AgentStartedEvent(_EventBase):
    """A sub-agent started running a task."""

    type: Literal["agent_started"] = Field(default="agent_started", frozen=True)
    agent: str = Field(description="Name of the sub-agent that started.")
    parent: str | None = Field(
        default=None, description="Path of the agent that spawned it. Null for a top-level spawn."
    )
    run_id: str | None = Field(
        default=None, description="Identifier of the sub-agent's run, when known."
    )
    task: str | None = Field(
        default=None, description="Task text given to the sub-agent, when known."
    )


class AgentPausedEvent(_EventBase):
    """A sub-agent paused because it needs user input (e.g. a tool approval)."""

    type: Literal["agent_paused"] = Field(default="agent_paused", frozen=True)
    count: int = Field(default=1, description="Number of pending requests the agent is waiting on.")


class AgentCompletedEvent(_EventBase):
    """A sub-agent finished its run, with its token usage."""

    type: Literal["agent_completed"] = Field(default="agent_completed", frozen=True)
    is_error: bool = Field(
        default=False, description="True when the sub-agent ended with an error."
    )
    input_tokens: int = Field(default=0, description="Input (prompt) tokens the sub-agent used.")
    output_tokens: int = Field(
        default=0, description="Output (completion) tokens the sub-agent produced."
    )
    reasoning_tokens: int = Field(
        default=0, description="Reasoning tokens the sub-agent used; 0 when the model reports none."
    )


class RunErrorEvent(_EventBase):
    """A sub-agent's run failed."""

    type: Literal["run_error"] = Field(default="run_error", frozen=True)
    error: str = Field(description="Error message describing the failure.")


class ContentPreviewEvent(_EventBase):
    """A short preview of the text a sub-agent is producing."""

    type: Literal["content_preview"] = Field(default="content_preview", frozen=True)
    text: str = Field(description="Preview text of the sub-agent's latest output.")


class TaskCreatedEvent(_EventBase):
    """A task was added to a team's task list."""

    type: Literal["task_created"] = Field(default="task_created", frozen=True)
    title: str = Field(default="", description="Task title. Empty when not given.")
    assignee: str = Field(
        default="", description="Name of the team member assigned the task. Empty when unassigned."
    )


class TaskUpdatedEvent(_EventBase):
    """A task in a team's task list changed status."""

    type: Literal["task_updated"] = Field(default="task_updated", frozen=True)
    status: str = Field(
        default="",
        description="New task status, such as `running`, `completed` or `failed`. Empty when unknown.",
    )


class VisualizationDeltaEvent(BaseModel):
    """One ``visualization_delta`` wire event. Sent to the FE either
    as a progressive delta (partial JSON as the visualizer sub-agent
    streams its tool_call arguments) or a final one carrying the
    fully-parsed spec (``final=True``) once ``ToolCallStartedEvent``
    fires.

    The FE partial-parses ``spec_json`` on every delta and swaps in
    the completed spec when ``final`` is true. ``spec_id`` dedupes
    so multiple deltas for the same card update in place instead of
    stacking. The wire uses key name ``json`` (kept via alias) to
    match the existing FE contract without renaming the field there.

    Not a subclass of :class:`_EventBase` because the wire shape is
    older and doesn't carry ``card_id`` — the FE routes on
    ``spec_id`` instead.
    """

    model_config = ConfigDict(populate_by_name=True)

    type: Literal["visualization_delta"] = Field(default="visualization_delta", frozen=True)
    agent_path: str
    spec_id: str
    spec_json: str = Field(alias="json")
    final: bool = False

    @classmethod
    def from_partial_args(
        cls,
        *,
        agent_path: str,
        spec_id: str,
        args_partial: str,
        final: bool = False,
    ) -> VisualizationDeltaEvent | None:
        """Build a delta event from a partial JSON tool-call arg string.

        Given the visualizer sub-agent's streaming ``arguments_partial``
        (e.g. ``'{"spec": {"root": "r", "elem'``), extract the
        ``spec`` sub-object and wrap it in a
        :class:`VisualizationDeltaEvent`. Uses ``jiter.from_json``
        with ``partial_mode='trailing-strings'``: tolerantly parses
        the incomplete outer object, salvaging as much nested
        structure as landed.

        Returns ``None`` when ``spec`` isn't a dict yet (first few
        tokens before the object opens) — callers can skip emission
        without an explicit try/except.
        """
        if not args_partial:
            return None
        try:
            parsed = jiter.from_json(args_partial.encode(), partial_mode="trailing-strings")
        except Exception:
            return None
        if not isinstance(parsed, dict):
            return None
        spec = parsed.get("spec")
        if not isinstance(spec, dict):
            return None
        return cls(
            agent_path=agent_path,
            spec_id=spec_id,
            spec_json=json.dumps(spec),
            final=final,
        )


# ── Log-line symbol table ───────────────────────────────────────────


class LogSymbols(str, Enum):
    """Named tree-drawing and status characters used in the
    parent-recap activity log.

    Rule 3 boundary: the characters themselves are load-bearing
    (Claude-Code-parity UX, and :meth:`AgentSpawn.format_result`
    parses the activity log for ``RUN ERROR``). Consumers reference
    the named enum member rather than the raw literal so a rename
    is one edit.
    """

    PAUSE = "⏸"
    COMPLETED = "✓"
    FAILED = "✗"
    RUNNING = "…"
    WARNING = "⚠"

    T_BRANCH = "├─"
    T_TRUNK = "│"
    T_ROOT = "┌─"
    T_JOIN = "╞═"
    T_LEAF = "└─"


TASK_STATUS_ICONS: dict[str, str] = {
    "completed": LogSymbols.COMPLETED.value,
    "failed": LogSymbols.FAILED.value,
    "running": LogSymbols.RUNNING.value,
}


# ── Collaborator protocols ──────────────────────────────────────────


OnProgress = Callable[[dict[str, Any]], None]
"""Structural type for the ``on_progress`` callback. The handler
converts a :class:`_EventBase` to a plain dict via
``model_dump(exclude_none=True, by_alias=True)`` before calling —
downstream code stays dict-oriented for wire encoding."""


EventAppender = Callable[[VisualizationDeltaEvent, str], Awaitable[None]]
"""Structural type for the session event-log appender.

Signature: ``async (delta, run_id) -> None`` — the viz card to log
and the run that emitted it. The delta travels as the typed wire
event, not as a free-form ``(type, payload)`` pair, so the log
receives a shape it can validate. Wired at session bootstrap;
``None`` in unit-test contexts where the session broadcast plumbing
isn't wired up.
"""


@runtime_checkable
class HitlCoordinatorProtocol(Protocol):
    """Structural type for the sub-agent HITL bridge.

    ``push_requirement`` returns a ``req_id`` the handler holds to
    later ``wait_resolved`` / ``cleanup``. Implemented by
    :class:`igni_core.core.sub_agent_hitl.SubAgentHITLCoordinator`.
    """

    async def push_requirement(
        self, req: Any, *, run_id: str, agent_path: list[str], session_id: str = ""
    ) -> str: ...

    async def wait_resolved(self, req_id: str) -> None: ...

    def cleanup(self, req_id: str) -> None: ...

    def forget_spawn(self, run_id: str) -> None: ...


@runtime_checkable
class AgnoRunnable(Protocol):
    """Structural type for the ``agent`` / ``team`` argument passed
    into the stream handlers.

    Every Agno ``Agent`` and ``Team`` satisfies this — plus the test
    fakes in ``tests/test_tool_arg_streaming.py`` and
    ``tests/test_subagent_hitl_e2e.py``.
    """

    def arun(self, task: str, stream: bool = True) -> Any: ...

    def acontinue_run(
        self, *, run_id: str, session_id: Any, requirements: Any, stream: bool
    ) -> Any: ...

    async def aget_run_output(self, *, run_id: str, session_id: str) -> Any: ...

    async def aget_last_run_output(self, *, session_id: str) -> Any: ...


# ── Visualizer spec hand-back ───────────────────────────────────────

#: Activity-log prefix carrying a rendered ``json-render`` spec back
#: to whoever spawned the visualizer.
#:
#: The spec is streamed to the client as a chat card, and the caller
#: used to receive only the visualizer's prose — "Rendered a 6-element
#: 'Database Migration' card". Fine for an orchestrator that just
#: wanted the user to see something; useless to an agent that has to
#: put the spec *in a document*, where the document's viewer renders
#: the fence live. A description of a chart cannot be stored
#: as one.
#:
#: The activity log is the channel because it already exists and is
#: already read this way: ``AgentSpawn.format_result`` scans it for
#: ``RUN ERROR`` and lifts what it finds into the parent's result.
#: This is the same move with a different prefix, which is why it
#: needs no signature change anywhere.
VIS_SPEC_PREFIX: str = "VISUALIZE SPEC "


# ── Cancellation registry ───────────────────────────────────────────


class SubAgentRegistry:
    """Per-``OrchestrateTools`` registry of in-flight sub-agent run
    ids.

    Agno's cooperative cancel (``Agent.cancel_run(run_id)``) flags
    exactly one run_id — the top-level team's cancel does not
    propagate to sub-agents Agno assigned distinct run_ids
    (visualizer, editor, every specialist). ``BackendServer.cancel_run``
    iterates this registry and cancels every entry so a stuck
    sub-agent (e.g. a visualizer retrying a truncated tool call in
    a loop) actually stops when the user hits ESC.

    Adds happen inside :class:`BaseStreamHandler` on the first event
    carrying a ``run_id``; removes happen in the outer ``try/finally``
    so a mid-stream exception still cleans up.

    The class replaces the previous
    ``OrchestrateTools._active_subagent_runs`` classvar — an
    injectable instance-scoped collaborator is easier to reason about
    under concurrent test runs and eliminates the audit's
    "classvar-used-as-singleton" offender.
    """

    __slots__ = ("_active",)

    def __init__(self, initial: Iterable[str] | None = None) -> None:
        self._active: set[str] = set(initial or ())

    def register(self, run_id: str) -> None:
        if run_id:
            self._active.add(run_id)

    def discard(self, run_id: str) -> None:
        self._active.discard(run_id)

    def __contains__(self, run_id: object) -> bool:
        return run_id in self._active

    def __iter__(self):
        return iter(tuple(self._active))

    def __len__(self) -> int:
        return len(self._active)

    def snapshot(self) -> set[str]:
        """Copy of the live set — safe to iterate while cancels fire."""
        return set(self._active)

    def clear(self) -> None:
        self._active.clear()


# ── Result envelope for the DB-fallback finalizer ───────────────────


class FinalizeResult(BaseModel):
    """Pydantic envelope for
    :meth:`BaseStreamHandler._fetch_final_content_with_fallback`.

    Replaces the broad ``except Exception`` block with a typed
    return: expected DB-not-yet-flushed failures land as
    ``FinalizeResult(content=None, error="...")``. Unexpected bugs
    bubble as ordinary exceptions (Pattern 3 — Result over
    raise-catch, for expected failures only).
    """

    model_config = ConfigDict(populate_by_name=True)

    content: str | None = None
    error: str | None = None
    status: str | None = None
    found: bool = False


# ── Hook payload models (typed replacements for raw dicts) ──────────
#
# ``OrchestrateTools._fire_hook`` used to build a plain
# ``dict[str, Any]`` for every SubagentStart / SubagentStop event. The
# start-vs-stop shapes are structurally different (start carries
# ``task``, stop carries ``result_preview`` or ``error``), so they
# live as two separate models — the "wide model with everything
# optional" collapse would defeat the type safety. The hook executor
# takes them as they are and its handlers put them on the wire.


class SubagentStartPayload(BaseModel):
    """Wire payload for the ``SubagentStart`` hook event.

    Fired at the top of every ``spawn_agent`` / ``spawn_team`` right
    after the budget reservation. Consumers (audit log, telemetry)
    see agent-name + truncated task; ``mode`` is populated for team
    spawns and stays ``None`` for single-agent spawns.
    """

    model_config = ConfigDict(populate_by_name=True)

    session_id: str
    agent_name: str
    task: str
    mode: str | None = None


class SubagentStopPayload(BaseModel):
    """Wire payload for the ``SubagentStop`` hook event.

    Fired in every exit path of a spawn — success, timeout, and
    the generic ``except``. Exactly one of ``result_preview`` /
    ``error`` is populated per event; the other stays ``None``.
    """

    model_config = ConfigDict(populate_by_name=True)

    session_id: str
    agent_name: str
    result_preview: str | None = None
    error: str | None = None


# ── Result envelopes for orchestrate collaborators ──────────────────


class SubTeamBuildResult(BaseModel):
    """Pydantic Result envelope for :meth:`SpawnRunner._build_sub_team`.

    Replaces the old ``str | tuple[Team, str]`` union return: an
    unknown agent name produces ``SubTeamBuildResult(team=None,
    mode=None, error='...')``; success carries the built
    :class:`Team` and the resolved mode (unknown modes are
    normalised to ``"coordinate"``).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    team: Any = None
    mode: str | None = None
    error: str | None = None


class SandboxSetupResult(BaseModel):
    """Pydantic Result envelope for :meth:`SpawnSandbox.create`.

    Replaces the old ``str | tuple[Any, WorktreeInfo|None, dict, str]``
    union: worktree creation failures land as
    ``SandboxSetupResult(sandbox=None, error='...')`` and the caller
    surfaces the error string to the agent. Success wraps the
    populated :class:`SpawnSandbox` instance plus the
    worktree-preamble ``task``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    sandbox: Any = None
    task: str = ""
    error: str | None = None


class AgentBuildResult(BaseModel):
    """Pydantic Result envelope for :meth:`AgentSpawn._build_agent`.

    Mirrors :class:`SubTeamBuildResult` for the single-agent shape:
    unknown agent names / worktree creation failures land as
    ``AgentBuildResult(agent=None, error='...')``; success wraps the
    per-spawn shallow copy of the agent, the resolved
    :class:`AgentDefinition`, the effective isolation mode (which may
    have been forced to ``"worktree"`` by ``force_isolation``), and
    the sandbox instance produced by :meth:`SpawnSandbox.create` so
    the caller can stash it for later finalize.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    agent: Any = None
    defn: Any = None
    isolation: str = ""
    sandbox: Any = None
    task: str = ""
    error: str | None = None


class SpawnDeps(BaseModel):
    """Injected dependency bundle for :class:`SpawnRunner`.

    Carries the four late-bound module-attribute references that
    ``SpawnRunner`` and its subclasses need to reach Agno's :class:`Team`
    factory, the :class:`ModelRegistry` factory, and the two streaming
    coroutines. The facade (:class:`OrchestrateTools`) is the ONE
    permitted site that reads these off the :mod:`orchestrate` module
    at call time (Rule 2 carve-out for genuine circular-import breaks —
    :mod:`orchestrate` imports :mod:`orchestrate_spawn`, so the reverse
    static import isn't allowed).

    Tests still patch ``igni_core.core.tools.orchestrate._run_agent_streaming``
    / ``.Team`` / ``.ModelRegistry`` at setup and the facade re-reads
    those attributes each call, so patches take effect exactly as they
    did with the old three-in-function-import scheme.

    Fields are typed ``Any`` (with ``arbitrary_types_allowed``) — same
    precedent as :attr:`SubTeamBuildResult.team`. A tighter
    ``Callable[..., Awaitable[Any]]`` would need a Pydantic
    ``model_rebuild`` because ``from __future__ import annotations``
    is on, and the runtime callable/class objects here are opaque
    enough that ``Any`` communicates the intent without buying
    validation we can't use.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    agent_streamer: Any
    team_streamer: Any
    team_factory: Any
    model_registry_factory: Any


class SpawnResult(BaseModel):
    """Typed envelope for the exception path of :meth:`SpawnRunner.run`.

    :class:`SpawnRunner.run` returns ``str`` (hard Agno tool-return
    contract), but the internal boundary between the failing branch
    and :meth:`SpawnRunner._render_error` is typed via this envelope
    so the generic ``except Exception`` doesn't drop information on
    the floor (audit Pattern 3 — Result over raise-catch for expected
    failures).
    """

    model_config = ConfigDict(populate_by_name=True)

    ok: bool = False
    message: str = ""


# ── Enums for spawn-time modes ──────────────────────────────────────


class TeamMode(str, Enum):
    """Valid Agno team modes for :meth:`TeamSpawn`.

    Unknown modes coerce silently to :attr:`COORDINATE` via
    :meth:`coerce_or_default` — preserved behaviour: the pre-refactor
    code did ``mode if mode in _VALID_MODES else "coordinate"``.
    """

    ROUTE = "route"
    COORDINATE = "coordinate"
    BROADCAST = "broadcast"
    TASKS = "tasks"

    @classmethod
    def coerce_or_default(cls, raw: str | None, default: TeamMode | None = None) -> TeamMode:
        """Silent-coerce a raw string to a :class:`TeamMode`.

        Unknown values fall back to ``default`` (``COORDINATE`` when
        omitted). Named ``coerce_or_default`` to distinguish it from
        :meth:`IsolationMode.parse_or_error` — the two enums have
        deliberately divergent policies (silent coerce vs. surfacing
        an error string) and the differing names are the safeguard.
        """
        fallback = default if default is not None else cls.COORDINATE
        if raw is None:
            return fallback
        try:
            return cls(raw)
        except ValueError:
            return fallback


class IsolationMode(str, Enum):
    """Valid isolation modes for :meth:`AgentSpawn`.

    ``NONE`` is the empty string (no isolation) — matches the historic
    default-arg surface of ``spawn_agent(isolation="")``. ``WORKTREE``
    forks a git worktree. Unknown modes surface an error string via
    :meth:`parse_or_error` — preserved behaviour from the previous
    ``_VALID_ISOLATION_MODES`` check.
    """

    NONE = ""
    WORKTREE = "worktree"

    @classmethod
    def parse_or_error(cls, raw: str | None) -> IsolationMode | str:
        """Parse a raw isolation string, returning an error message on
        unknown values.

        Distinguished from :meth:`TeamMode.coerce_or_default` by name
        so a maintainer can't accidentally pick the wrong policy at a
        call site.
        """
        if raw is None:
            return cls.NONE
        try:
            return cls(raw)
        except ValueError:
            valid = sorted(m.value for m in cls if m.value)
            return f"Error: unknown isolation mode {raw!r}. Valid: {valid}."
