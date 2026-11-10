"""One sub-team, spawned.

:class:`TeamSpawn` is the multi-agent flavour of
:class:`SpawnRunner`: it draws the members out of the pool, gives
each of them a per-spawn copy so concurrent spawns of the same
specialist don't race on Agno's per-run state, assembles them into an
Agno team, and streams it to completion.

A team is not isolated the way a single agent can be — that is a
per-member concern, and the audit's Pattern 8 split keeps team spawns
sandbox-free.

What it knows about itself lives in one
:class:`TeamSpawnState` model, and the collaborators it needs arrive
in the spawn's :class:`SpawnContext`.
"""

from __future__ import annotations

import copy
from typing import Any

from igni_core.core.agno_telemetry import AgnoTelemetry
from igni_core.core.hooks.untrusted import UntrustedOutputHook
from igni_core.core.tools.orchestrate_modes import TeamMode
from igni_core.core.tools.orchestrate_spawn_runner import SpawnRunner
from igni_core.core.tools.orchestrate_spawn_types import (
    DEFAULT_TEAM_MAX_ITERATIONS,
    SpawnContext,
    SpawnDeps,
    SubTeamBuildResult,
    TeamBuildRequest,
    TeamSpawnState,
    TeamSpec,
)
from igni_core.core.tools.orchestrate_stream_context import StreamContext


class TeamSpawn(SpawnRunner):
    """Sub-team spawn.

    Builds an Agno team from a name list plus a mode, and delegates
    streaming to the injected ``team_streamer``.
    """

    def __init__(self, *, context: SpawnContext, names: list[str], mode: str) -> None:
        super().__init__(context)
        self._state = TeamSpawnState(names=list(names), mode_raw=mode)

    # ── SpawnRunner extension points ─────────────────────────────

    def _preflight(self) -> str | None:
        if not self._state.names:
            return "Error: No agent names provided."
        return None

    def _reserve_count(self) -> int:
        """A team costs one budget slot per member.

        The cap is on how many sub-agents may run, and a team of four
        runs four of them — so four slots, not one.
        """
        return len(self._state.names)

    def _build(self) -> str | None:
        built = self._build_sub_team()
        if built.error is not None:
            return built.error
        state = self._state
        state.team = built.team
        state.mode = built.mode or TeamMode.COORDINATE
        state.member_lines = self._member_lines()
        state.team_label = f"team({state.mode.value}:{','.join(state.names)})"
        return None

    def _build_sub_team(self) -> SubTeamBuildResult:
        """Assemble the sub-team this spawn runs.

        Mirrors :meth:`AgentSpawn._build_agent` so both spawn shapes
        have a symmetric build method.
        """
        scope = self._context.scope
        return self.build_team(
            TeamBuildRequest(
                pool=scope.pool,
                settings=scope.settings,
                names=list(self._state.names),
                mode=self._state.mode_raw,
                depth=scope.current_depth + 1,
            ),
            self._context.deps,
        )

    @staticmethod
    def build_team(request: TeamBuildRequest, deps: SpawnDeps) -> SubTeamBuildResult:
        """The Agno team a ``spawn_team`` call runs.

        Also how a team a restart cut off is rebuilt so it can be
        continued (``backend/cold_spawn_resume.py``) — the same team,
        built the same way.

        Members get per-spawn shallow copies — see
        :meth:`AgentSpawn._build_agent` for the rationale. Sub-team
        members race on shared per-run state without the copy.

        The team shares the session's DB with the pool so Agno's
        ``team.acontinue_run(run_id, session_id)`` can resolve the
        run — without it, broadcast_* / single_specialist_* cases
        hit "No runs found for run ID …".
        """
        members: list[Any] = []
        for name in request.names:
            shared = request.pool.get(name)
            if shared is None:
                return SubTeamBuildResult(team=None, mode=None, error=request.pool.missing(name))
            member = copy.copy(shared)
            member.tool_hooks = UntrustedOutputHook.on(getattr(member, "tool_hooks", None))
            members.append(member)

        mode = TeamMode.parse(request.mode)
        max_iterations = DEFAULT_TEAM_MAX_ITERATIONS
        if mode is TeamMode.TASKS:
            # Only the ``tasks`` mode has an iteration loop, and only
            # it reads the cap.
            max_iterations = request.settings.orchestration.max_task_iterations

        spec = TeamSpec(
            name=f"sub-team-depth-{request.depth}",
            mode=mode,
            members=members,
            model=deps.model_registry_factory(request.settings).get_model(),
            db=request.pool.db,
            markdown=True,
            # Agno's run telemetry to os-api.agno.com (core/agno_telemetry.py).
            telemetry=AgnoTelemetry.CONSTRUCTOR_VALUE,
            max_iterations=max_iterations,
        )
        return SubTeamBuildResult(team=deps.team_factory(spec), mode=mode, error=None)

    def _member_lines(self) -> list[str]:
        """One line per member, for the response header."""
        pool = self._context.scope.pool
        lines: list[str] = []
        for name in self._state.names:
            entry = pool.get_entry(name)
            description = entry.definition.description[:60] if entry is not None else ""
            lines.append(f"  - {name}: {description}")
        return lines

    async def _execute(self) -> tuple[str, list[str]]:
        context = StreamContext(
            runnable=self._state.team,
            task=self._context.task,
            # The team's own label: the card shows the team, and the
            # per-member events add the member under it.
            agent_path=[self._state.team_label],
            card_id=self._hooks.card_id,
            sinks=self._context.wiring.sinks,
        )
        return await self._context.deps.team_streamer(context)

    def _agent_label(self) -> str:
        return f"team({','.join(self._state.names)})"

    def _start_mode(self) -> str | None:
        return self._state.mode.value

    def format_result(self, *, elapsed: float, result: str, activity: list[str]) -> str:
        """Header + response only — see
        :meth:`AgentSpawn.format_result` for the rationale.

        The orchestrator only consumes (task, response); the
        intermediate activity of team members is noise at that level
        and lives instead in the UI event stream via ``event_appender``.
        """
        del activity  # surfaced via event_appender, not the parent's tool result
        header = self._header(elapsed)
        return f"{header}\n\nResponse:\n{result}"

    def _header(self, elapsed: float) -> str:
        state = self._state
        return (
            f"[Team: {', '.join(state.names)}] (mode: {state.mode.value})\n"
            f"[Members:\n" + "\n".join(state.member_lines) + "]\n"
            f"[Task: {self._context.task}]\n"
            f"[Time: {elapsed:.1f}s]"
        )

    def _timeout_message(self) -> str:
        return (
            f"Sub-team {self._state.team_label!r} exceeded spawn timeout "
            f"({self._spawn_timeout}s) and was aborted."
        )

    def _exception_message(self, exc: Exception) -> str:
        return f"Error running sub-team: {exc}"


__all__ = ["TeamSpawn"]
