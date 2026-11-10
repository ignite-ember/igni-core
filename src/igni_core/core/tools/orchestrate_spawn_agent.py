"""One specialist agent, spawned.

:class:`AgentSpawn` is the single-agent flavour of
:class:`SpawnRunner`: it draws the agent out of the pool, gives it a
per-spawn copy so concurrent spawns of the same specialist don't race
on Agno's per-run state, optionally forks a git worktree for it to
work in, and streams it to completion.

What it knows about itself lives in one
:class:`AgentSpawnState` model rather than in a dozen attributes, and
the collaborators it needs arrive in the spawn's
:class:`SpawnContext`.
"""

from __future__ import annotations

import copy
from typing import Any

from igni_core.core.hooks.turn_results import TurnResultHook, TurnResultSink
from igni_core.core.hooks.untrusted import UntrustedOutputHook
from igni_core.core.tools.orchestrate_modes import IsolationMode
from igni_core.core.tools.orchestrate_sandbox import SpawnSandbox
from igni_core.core.tools.orchestrate_spawn_runner import SpawnRunner
from igni_core.core.tools.orchestrate_spawn_types import (
    AgentBuildResult,
    AgentSpawnState,
    NestedToolkitSpec,
    SpawnContext,
    SpawnResult,
)
from igni_core.core.tools.orchestrate_stream_context import StreamContext
from igni_core.core.tools.orchestrate_symbols import VIS_SPEC_PREFIX
from igni_core.core.tools.spawn_context import SpawnCalls


class AgentSpawn(SpawnRunner):
    """Single-agent spawn.

    Owns a :class:`SpawnSandbox` for optional worktree isolation.
    """

    def __init__(self, *, context: SpawnContext, agent_name: str, isolation: str) -> None:
        super().__init__(context)
        self._state = AgentSpawnState(
            agent_name=agent_name,
            isolation_raw=isolation,
            effective_task=context.task,
        )

    # ── SpawnRunner extension points ─────────────────────────────

    def _preflight(self) -> str | None:
        parsed = IsolationMode.parse(self._state.isolation_raw)
        if parsed is None:
            # An unknown isolation mode is a refusal, not a default:
            # the caller asked for a sandbox and would otherwise
            # silently get none.
            return IsolationMode.unknown_message(self._state.isolation_raw)
        self._state.isolation = parsed
        return None

    def _build(self) -> str | None:
        result = self._build_agent()
        if result.error is not None:
            return result.error
        state = self._state
        state.agent = result.agent
        state.description = result.defn.description if result.defn else ""
        state.tools = ", ".join(result.defn.tools) if result.defn and result.defn.tools else "none"
        state.effective_task = result.task
        state.sandbox = result.sandbox
        return None

    def _build_agent(self) -> AgentBuildResult:
        """Assemble the per-spawn agent copy + sandbox.

        Mirrors :meth:`TeamSpawn._build_sub_team` so both spawn
        shapes have a symmetric build method. Returns a typed
        :class:`AgentBuildResult` envelope: unknown agent names /
        worktree creation failures land as
        ``AgentBuildResult(agent=None, error='...')``.
        """
        pool = self._context.scope.pool
        agent_name = self._state.agent_name
        shared = pool.get(agent_name)
        if shared is None:
            return AgentBuildResult(agent=None, error=pool.missing(agent_name))

        # Shallow-copy per spawn. Agno ``Agent`` instances hold per-
        # run state (``run_id``, ``session_id``, ``run_response``);
        # the pool caches one instance per agent name and hands it to
        # every caller. Concurrent spawns of the same specialist race
        # on that state without a per-spawn copy.
        agent = copy.copy(shared)
        # Its own list: the copy is shallow, and the pool's instance must
        # not collect a hook per spawn. Its tool output is quarantined
        # whatever built it (the pool's agents already carry the hook).
        agent.tool_hooks = UntrustedOutputHook.on(getattr(agent, "tool_hooks", None))
        coordinator = self._context.wiring.sinks.hitl_coordinator
        if isinstance(coordinator, TurnResultSink):
            agent.tool_hooks = [*agent.tool_hooks, TurnResultHook(coordinator)]

        entry = pool.get_entry(agent_name)
        defn = entry.definition if entry is not None else None
        self._grant_nested_orchestration(agent, defn)

        # Plugin-shipped agents force their own isolation regardless
        # of what the caller asked for — CC parity row 37.
        isolation = self._state.isolation
        if defn is not None and defn.force_isolation == "worktree":
            isolation = IsolationMode.WORKTREE

        setup = SpawnSandbox.create(
            project_dir=self._context.scope.project_dir,
            session_id=self._context.wiring.session_id,
            agent=agent,
            agent_name=agent_name,
            isolation=isolation,
            task=self._context.task,
        )
        if setup.error is not None:
            return AgentBuildResult(agent=None, defn=defn, error=setup.error)

        return AgentBuildResult(
            agent=agent,
            defn=defn,
            isolation=isolation,
            sandbox=setup.sandbox,
            task=setup.task,
        )

    def _grant_nested_orchestration(self, agent: Any, defn: Any) -> None:
        """Give a spawned agent its own spawn tools, one level down.

        ``can_orchestrate`` was declared in :class:`AgentDefinition`
        and set deliberately across twenty agent files long before
        anything read it — only a panel ever did, so an agent marked
        ``true`` could not in fact delegate. This is the read.

        The depth it is built at is ``current_depth + 1``, which is
        what makes ``orchestration.max_nesting_depth`` start counting.
        At the last level the grant is **withheld** rather than made
        and refused: a manifest carrying a tool whose every call
        returns "maximum nesting depth reached" is the same defect as
        a prompt naming a tool that was never registered.

        Nothing is appended in place. ``copy.copy`` above is shallow,
        so ``agent.tools`` is still the list the pool's cached
        instance holds — appending would hand the toolkit to every
        later spawn of that agent, at this spawn's depth.
        """
        if defn is None or not getattr(defn, "can_orchestrate", False):
            return
        scope = self._context.scope
        depth = scope.current_depth + 1
        if depth >= scope.max_depth:
            return

        nested = self._context.deps.nested_toolkit_factory(
            NestedToolkitSpec(
                depth=depth,
                scope=scope.model_copy(update={"current_depth": depth}),
                wiring=self._context.wiring,
            )
        )
        agent.tools = [*(agent.tools or []), nested]

    async def _execute(self) -> tuple[str, list[str]]:
        context = StreamContext(
            runnable=self._state.agent,
            task=self._state.effective_task,
            # Nested spawns name every agent between the main agent and
            # this one, so the person sees who is asking.
            agent_path=SpawnCalls.path_to(self._state.agent_name),
            card_id=self._hooks.card_id,
            sinks=self._context.wiring.sinks,
        )
        return await self._context.deps.agent_streamer(context)

    def _agent_label(self) -> str:
        return self._state.agent_name

    def _on_started_progress(self) -> None:
        self._hooks.emit_agent_started(self._state.agent_name, self._context.task)

    def format_result(self, *, elapsed: float, result: str, activity: list[str]) -> str:
        """Compose the four-line header + response +
        (optional) run-error warning + worktree footer.

        The orchestrator only needs the sub-agent's INPUT (task, already in
        the header) and OUTPUT (response). Its internal tool_calls are
        useless context noise — the orchestrator can neither replay nor
        debug them, and every token spent replaying activity is a token
        stolen from real reasoning. Prior versions included an ``Activity:``
        block; ``activity`` is still surfaced via ``event_appender`` for
        UI/logging consumers.
        """
        header = self._header(elapsed)
        run_errors = [line for line in activity if "RUN ERROR" in line]
        error_section = ""
        if run_errors:
            error_section = (
                "\n\nWARNING: This sub-agent terminated with a run error — "
                "the response below is partial. Consider retrying, or proceed "
                "with the partial result if it's sufficient.\n" + "\n".join(run_errors)
            )
        sandbox = self._state.sandbox
        worktree_footer = sandbox.finalize() if sandbox else ""
        return (
            f"{header}\n\nResponse:\n{result}"
            f"{self._rendered_specs(activity)}{error_section}{worktree_footer}"
        )

    @staticmethod
    def _rendered_specs(activity: list[str]) -> str:
        """Any visualization the sub-agent drew, as a fence.

        Fenced as ``json-render`` rather than handed over as bare
        JSON because that is the form it has to end up in: a document
        carrying this fence renders the chart wherever it is shown,
        and a caller that can copy the block through verbatim cannot
        get the shape wrong on the way. The alternative asks a model
        to re-serialise a forty-element spec from a description of
        it, which is where invented data comes from.
        """
        specs = [
            line[len(VIS_SPEC_PREFIX) :] for line in activity if line.startswith(VIS_SPEC_PREFIX)
        ]
        if not specs:
            return ""
        blocks = "\n\n".join(f"```json-render\n{spec}\n```" for spec in specs)
        return (
            "\n\nRendered for you — the block below is the visualization, "
            "ready to paste into a document as-is:\n\n" + blocks
        )

    def _header(self, elapsed: float) -> str:
        state = self._state
        return (
            f"[Agent: {state.agent_name}] {state.description}\n"
            f"[Tools: {state.tools}]\n"
            f"[Task: {self._context.task}]\n"
            f"[Time: {elapsed:.1f}s]"
        )

    def _on_finally(self) -> None:
        # The template's ``finally`` fires this from every exit path,
        # including cancellation — which is the only path where nothing
        # else finalizes, and so the one that used to leak the worktree
        # and its branch. ``format_result`` (success) and
        # ``_render_error`` (timeout / exception) have already finalized
        # by the time we get here; ``finalize()`` is documented
        # idempotent and exception-safe, so the second call is a no-op.
        sandbox = self._state.sandbox
        if sandbox is not None:
            sandbox.finalize()

    def _timeout_message(self) -> str:
        return (
            f"Sub-agent '{self._state.agent_name}' exceeded spawn timeout "
            f"({self._spawn_timeout}s) and was aborted. The model "
            "provider likely stalled mid-stream."
        )

    def _exception_message(self, exc: Exception) -> str:
        return f"Error running sub-agent '{self._state.agent_name}': {exc}"

    def _render_error(self, envelope: SpawnResult) -> str:
        # Sandbox finalize on error paths — matches the pre-refactor
        # behaviour where every ``except`` branch called finalize
        # before returning the error string.
        sandbox = self._state.sandbox
        if sandbox is not None:
            sandbox.finalize()
        return envelope.message


__all__ = ["AgentSpawn"]
