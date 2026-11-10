"""Team stream handler — one Agno team, one stream, N members.

Concrete subclass of :class:`BaseStreamHandler` for the ``spawn_team``
path. Sibling of :class:`SubAgentStreamHandler`.

The team case adds:

* Per-member agent-path derivation via ``_agent_path_for`` /
  ``_event_agent_path`` — each Agno event carries its owning agent
  name, so tool events attribute to the right specialist even when
  members emit interleaved during broadcast mode.
* Per-member throttled content-preview via
  :meth:`TeamStreamState.append_content_delta`.
* Task lifecycle events (``TaskCreated`` / ``TaskUpdated`` /
  ``TaskIterationStarted``) — team-mode-only.

The team's own lifecycle and tool events are not handled at all: Agno's
Team runs its own coordinator model in coordinate/route/tasks modes,
and surfacing that coordinator as a fourth row beside three real
workers confuses users — the team-progress card already represents
the team. Leaving those events out of :data:`TEAM_EVENT_HANDLERS` is
what hides them.
"""

from __future__ import annotations

import logging
from typing import Any

from agno.run import agent as agent_events
from agno.run import team as team_events

from igni_core.core.tools.orchestrate_events import (
    AgentCompletedEvent,
    AgentPausedEvent,
    AgentStartedEvent,
    ContentPreviewEvent,
    RunErrorEvent,
    TaskCreatedEvent,
    TaskUpdatedEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
)
from igni_core.core.tools.orchestrate_preview import PREVIEWS
from igni_core.core.tools.orchestrate_stream_context import StreamContext
from igni_core.core.tools.orchestrate_stream_handler import (
    BaseStreamHandler,
    active_requirements,
    handler_name,
)
from igni_core.core.tools.orchestrate_symbols import TASK_STATUS_ICONS, LogSymbols
from igni_core.core.tools.subagent_stream import TeamStreamState
from igni_core.protocol.agno_compat import RunProducer

_stream_log = logging.getLogger("igni_core.llm_calls")


#: Which method answers which Agno event, for a team's stream. A
#: member's events are Agno's *agent* events; the team's own are its
#: *team* events, and the ones the team does not want to show — its
#: coordinator's lifecycle and tool calls — are simply absent.
TEAM_EVENT_HANDLERS: dict[type, str] = {
    agent_events.RunStartedEvent: "_on_agent_run_started",
    team_events.RunPausedEvent: "_on_run_paused",
    team_events.TaskCreatedEvent: "_on_task_created",
    team_events.TaskUpdatedEvent: "_on_task_updated",
    team_events.TaskIterationStartedEvent: "_on_task_iteration_started",
    agent_events.ToolCallStartedEvent: "_on_tool_started",
    agent_events.ToolCallCompletedEvent: "_on_tool_completed",
    agent_events.ToolCallErrorEvent: "_on_tool_error",
    agent_events.RunErrorEvent: "_on_run_error",
    agent_events.RunCompletedEvent: "_on_agent_run_completed",
    agent_events.RunContentEvent: "_on_run_content",
}


class TeamStreamHandler(BaseStreamHandler[TeamStreamState]):
    """Drive one Agno team stream."""

    def __init__(self, context: StreamContext) -> None:
        base_path = list(context.agent_path)
        super().__init__(
            context,
            TeamStreamState(
                team_path_id=".".join(base_path) if base_path else "team",
                card_id=context.card_id,
            ),
        )
        self._base_path = base_path

    def _latch_run_ids(self, event: Any) -> None:
        """Latch the *team's* run, not a member's.

        The team's stream starts with its members' events when the team
        itself streams no lifecycle events, and latching the first id
        seen took a member's run for the team's: the team's answer read
        back was that member's, and a continued pause found no run.
        """
        if isinstance(event, team_events.BaseTeamRunEvent):
            super()._latch_run_ids(event)

    # ── Dispatch ───────────────────────────────────────────────────
    async def _handle(self, event: Any) -> Any:
        """Return a follow-up async iterator on pause+resume, else
        ``None``."""
        self._latch_run_ids(event)

        name = handler_name(TEAM_EVENT_HANDLERS, event)
        if name is None:
            return None
        return await getattr(self, name)(event)

    # ── Helpers ────────────────────────────────────────────────────
    def _agent_path_for(self, name: str | None) -> str:
        """Build the agent-path id for a team member by appending
        its name to the team's base path."""
        if not name:
            return self.state.team_path_id
        base = self._base_path
        return ".".join(base + [name]) if base else name

    def _event_agent_path(self, event: Any) -> tuple[str, str]:
        """Pull the agent identity off ANY Agno event and return
        ``(path, display_name)``.

        Reading from the event itself — not a shared
        ``state.current_agent`` — is critical for broadcast runs
        where multiple sub-agents emit interleaved tool events:
        every tool call carries its owning ``agent_name`` (or
        ``team_name`` for nested teams), and using that prevents
        the "all tools land on the last started agent" bug.
        """
        name = RunProducer.from_agno(event).name or self.state.current_agent or ""
        return self._agent_path_for(name), name

    # ── Per-event handlers ─────────────────────────────────────────
    async def _on_agent_run_started(self, event: agent_events.RunStartedEvent) -> None:
        state = self.state
        name = RunProducer.from_agno(event).name
        if not name or name == state.current_agent:
            return
        state.current_agent = name
        self._log_line(f"  {LogSymbols.T_BRANCH.value} [{name}]")
        self._emit(
            AgentStartedEvent(
                agent_path=self._agent_path_for(name),
                agent=name,
                parent=state.team_path_id,
                # Emit the run_id so the FE can target this
                # specific sub-agent for cancellation —
                # ``cancel_agent_run(run_id)`` flags this run for
                # cooperative stop while siblings keep going.
                run_id=str(event.run_id or ""),
                # The team's task is what the parent agent asked
                # the broadcast to do. Each member receives the
                # same prompt — the FE Retry UI pre-fills it so
                # the user can tweak before respawning.
                task=self._context.task,
            )
        )

    async def _on_run_paused(self, event: team_events.RunPausedEvent) -> Any:
        state = self.state
        coordinator = self._context.sinks.hitl_coordinator
        reqs = active_requirements(event)
        if coordinator is None or not reqs:
            self._log_line(
                f"  {LogSymbols.T_TRUNK.value}  "
                f"{LogSymbols.WARNING.value} paused: no HITL bridge available"
            )
            return None
        run_id = event.run_id or state.current_run_id or ""
        session_id = event.session_id or state.current_session_id
        req_ids: list[str] = []
        for req in reqs:
            # If Agno tagged the requirement with a member name (set
            # for team-internal pauses), append it to the path so
            # the FE shows e.g. ``review-team → security``.
            member = getattr(req, "member_agent_name", None)
            req_path = self._base_path + [member] if member else self._base_path
            req_id = await coordinator.push_requirement(
                req, run_id=run_id, agent_path=req_path, session_id=session_id or ""
            )
            req_ids.append(req_id)
        pause_line = (
            f"  {LogSymbols.T_TRUNK.value}  {LogSymbols.PAUSE.value} "
            f"awaiting approval ({len(req_ids)} tools)"
            if len(req_ids) > 1
            else f"  {LogSymbols.T_TRUNK.value}  {LogSymbols.PAUSE.value} awaiting approval"
        )
        self._log_line(pause_line)
        self._emit(
            AgentPausedEvent(
                agent_path=self._agent_path_for(state.current_agent),
                count=len(req_ids),
            )
        )
        try:
            for req_id in req_ids:
                await coordinator.wait_resolved(req_id)
        finally:
            for req_id in req_ids:
                coordinator.cleanup(req_id)
        return self._context.runnable.acontinue_run(
            run_id=run_id,
            session_id=session_id,
            requirements=reqs,
            stream=True,
        )

    async def _on_task_created(self, event: team_events.TaskCreatedEvent) -> None:
        title = event.title or ""
        assignee = event.assignee or ""
        self._log_line(f"  {LogSymbols.T_ROOT.value} TASK: {title}")
        if assignee:
            self._log_line(f"  {LogSymbols.T_TRUNK.value}  assigned to: {assignee}")
        self._emit(
            TaskCreatedEvent(
                agent_path=self._agent_path_for(assignee or self.state.current_agent),
                title=title,
                assignee=assignee,
            )
        )

    async def _on_task_updated(self, event: team_events.TaskUpdatedEvent) -> None:
        status = event.status or ""
        icon = TASK_STATUS_ICONS.get(status, "·")
        self._log_line(f"  {LogSymbols.T_TRUNK.value}  {icon} {status}")
        self._emit(
            TaskUpdatedEvent(
                agent_path=self._agent_path_for(self.state.current_agent),
                status=status,
            )
        )

    async def _on_task_iteration_started(
        self, event: team_events.TaskIterationStartedEvent
    ) -> None:
        self._log_line(f"  {LogSymbols.T_JOIN.value} Iteration {event.iteration}")

    async def _on_tool_started(self, event: agent_events.ToolCallStartedEvent) -> None:
        ev_path, _name = self._event_agent_path(event)
        te = event.tool
        tn = (te.tool_name or "tool") if te else "tool"
        ta = te.tool_args if te else {}
        args_preview = PREVIEWS.format_args(ta)
        args_full = PREVIEWS.format_full_args(ta)
        self.state.current_tool = tn
        self._log_line(
            f"  {LogSymbols.T_TRUNK.value}  {LogSymbols.T_BRANCH.value} {tn}({args_full})"
        )
        tc_id = getattr(te, "tool_call_id", None) if te else None
        self._emit(
            ToolStartedEvent(
                agent_path=ev_path,
                tool=tn,
                tool_call_id=tc_id,
                args=args_preview,
            )
        )

    async def _on_tool_completed(self, event: agent_events.ToolCallCompletedEvent) -> None:
        state = self.state
        ev_path, _name = self._event_agent_path(event)
        te = event.tool
        r = getattr(te, "result", None) if te else None
        tn = (
            (te.tool_name or state.current_tool or "tool") if te else (state.current_tool or "tool")
        )
        tc_id = getattr(te, "tool_call_id", None) if te else None
        result_preview = PREVIEWS.format_result(r)
        result_full = "" if r is None else str(r)
        self._log_line(
            f"  {LogSymbols.T_TRUNK.value}  {LogSymbols.T_TRUNK.value}  "
            f"{LogSymbols.T_LEAF.value} {result_full}"
        )
        self._emit(
            ToolCompletedEvent(
                agent_path=ev_path,
                tool=tn,
                tool_call_id=tc_id,
                result=result_preview,
                is_error=False,
            )
        )
        if state.current_tool == tn:
            state.current_tool = None

    async def _on_tool_error(self, event: agent_events.ToolCallErrorEvent) -> None:
        state = self.state
        ev_path, _name = self._event_agent_path(event)
        te = getattr(event, "tool", None)
        tn = (te.tool_name if te else None) or state.current_tool or "tool"
        tc_id = getattr(te, "tool_call_id", None) if te else None
        err = str(getattr(event, "error", "?"))
        self._log_line(
            f"  {LogSymbols.T_TRUNK.value}  {LogSymbols.T_TRUNK.value}  "
            f"{LogSymbols.T_LEAF.value} ERROR: {err}"
        )
        self._emit(
            ToolCompletedEvent(
                agent_path=ev_path,
                tool=tn,
                tool_call_id=tc_id,
                result=err[:200],
                is_error=True,
            )
        )
        if state.current_tool == tn:
            state.current_tool = None

    async def _on_run_error(self, event: agent_events.RunErrorEvent) -> None:
        ev_path, _name = self._event_agent_path(event)
        err = str(getattr(event, "content", "?"))
        self._log_line(f"  {LogSymbols.T_TRUNK.value}  {LogSymbols.T_LEAF.value} ERROR: {err}")
        self._emit(
            RunErrorEvent(
                agent_path=ev_path,
                error=err[:400],
            )
        )

    async def _on_agent_run_completed(self, event: agent_events.RunCompletedEvent) -> None:
        state = self.state
        ev_path, _name = self._event_agent_path(event)
        state.record_completion(getattr(event, "content", None))
        # Pull per-agent token totals off ``event.metrics`` so the
        # team-progress card can show "N tokens" per agent and
        # sum them in the header.
        mt = getattr(event, "metrics", None)
        self._emit(
            AgentCompletedEvent(
                agent_path=ev_path,
                is_error=False,
                input_tokens=int(getattr(mt, "input_tokens", 0) or 0) if mt else 0,
                output_tokens=int(getattr(mt, "output_tokens", 0) or 0) if mt else 0,
                reasoning_tokens=int(getattr(mt, "reasoning_tokens", 0) or 0) if mt else 0,
            )
        )

    async def _on_run_content(self, event: agent_events.RunContentEvent) -> None:
        # Live progress preview only — final content comes from the
        # team's stored output after the stream ends.
        ev_path, _name = self._event_agent_path(event)
        chunk = event.content or ""
        preview = self.state.append_content_delta(ev_path, chunk)
        if preview is None:
            return
        self._emit(
            ContentPreviewEvent(
                agent_path=ev_path,
                text=preview,
            )
        )


__all__ = ["TEAM_EVENT_HANDLERS", "TeamStreamHandler"]
