"""Sub-agent stream handler — one specialist agent, one Agno stream.

Concrete subclass of :class:`BaseStreamHandler` for the ``spawn_agent``
path. The base class owns the outer loop, the cancellation registry and
the final read-back; this class owns per-event dispatch.

Dispatch is a table, not a ladder: :data:`AGENT_EVENT_HANDLERS` maps an
Agno event class to the method that answers it, and the lookup walks
the event's method resolution order, so a class that derives from a
handled event is answered by its parent's method. Adding an event type
is one row in the table and one method beside it.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from agno.run import agent as agent_events

from igni_core.core.tools.orchestrate_events import (
    AgentCompletedEvent,
    AgentPausedEvent,
    ContentPreviewEvent,
    RunErrorEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
    VisualizationDeltaEvent,
)
from igni_core.core.tools.orchestrate_preview import PREVIEWS
from igni_core.core.tools.orchestrate_stream_context import StreamContext
from igni_core.core.tools.orchestrate_stream_handler import (
    BaseStreamHandler,
    active_requirements,
    handler_name,
)
from igni_core.core.tools.orchestrate_symbols import VIS_SPEC_PREFIX, LogSymbols
from igni_core.core.tools.subagent_stream import SubAgentStreamState

_stream_log = logging.getLogger("igni_core.llm_calls")
_logger = logging.getLogger(__name__)


#: Which method answers which Agno event. The lookup walks the event's
#: method resolution order, so a class that derives from a handled
#: event is answered by its parent's method.
AGENT_EVENT_HANDLERS: dict[type, str] = {
    agent_events.RunPausedEvent: "_on_run_paused",
    agent_events.ToolCallStartedEvent: "_on_tool_started",
    agent_events.ToolCallCompletedEvent: "_on_tool_completed",
    agent_events.ToolCallErrorEvent: "_on_tool_error",
    agent_events.RunErrorEvent: "_on_run_error",
    agent_events.RunCompletedEvent: "_on_run_completed",
    agent_events.CustomEvent: "_on_custom_event",
    agent_events.RunContentEvent: "_on_run_content",
}


class SubAgentStreamHandler(BaseStreamHandler[SubAgentStreamState]):
    """Drive one Agno agent stream to completion.

    The handler is built from a :class:`StreamContext`: the agent to
    drive, the task to start it with, and the sinks it reports
    through. Its state is derived from the same context — the agent
    path is the context's path, the card id is the context's card id.
    """

    def __init__(self, context: StreamContext) -> None:
        path = list(context.agent_path)
        super().__init__(
            context,
            SubAgentStreamState(
                agent_path_id=".".join(path) if path else "root",
                card_id=context.card_id,
            ),
        )

    # ── Base-class hooks ───────────────────────────────────────────
    def _agent_completed_path(self) -> str:
        return self.state.agent_path_id

    def _shape_final(self, final: str) -> str:
        # For the visualizer sub-agent, the tool call already streamed
        # progressive ``visualization_delta`` events to the FE (see
        # ``_on_custom_event`` below), so the card is already
        # rendered. Return a short summary so the PARENT agent's
        # context doesn't get polluted with the raw spec JSON — the
        # parent just quotes the summary to the user.
        path = self._context.agent_path
        if path and path[-1] == "visualizer":
            return "Emitted visualization to the client."
        return final

    # ── Dispatch ───────────────────────────────────────────────────
    async def _handle(self, event: Any) -> Any:
        """Drive progress reporting and HITL bridging.

        We don't try to reassemble the model's text from streaming
        events — Agno already accumulates that into the run's content
        when the run ends. We just watch the stream for tool-call
        lifecycle (for activity log lines), HITL pauses (for the
        bridge), and content deltas (for the live progress preview).

        Returns a follow-up async iterator when we resumed the run
        after a pause; otherwise ``None``.
        """
        # Latch identifiers off every event that carries them —
        # RunStartedEvent-only capture misses the specialist path,
        # see :meth:`SubAgentStreamState.latch_ids`.
        self._latch_run_ids(event)

        name = handler_name(AGENT_EVENT_HANDLERS, event)
        if name is None:
            return None
        return await getattr(self, name)(event)

    # ── Per-event handlers ─────────────────────────────────────────
    async def _on_run_paused(self, event: agent_events.RunPausedEvent) -> Any:
        state = self.state
        coordinator = self._context.sinks.hitl_coordinator
        reqs = active_requirements(event)
        if coordinator is None or not reqs:
            self._log_line(
                f"  {LogSymbols.T_TRUNK.value}  "
                f"{LogSymbols.WARNING.value} paused: no HITL bridge available"
            )
            self._emit(
                RunErrorEvent(
                    agent_path=state.agent_path_id,
                    error="paused: no HITL bridge available",
                )
            )
            return None
        run_id = event.run_id or state.current_run_id or ""
        session_id = event.session_id or state.current_session_id
        req_ids: list[str] = []
        for req in reqs:
            req_id = await coordinator.push_requirement(
                req,
                run_id=run_id,
                agent_path=self._context.agent_path,
                session_id=session_id or "",
            )
            req_ids.append(req_id)
        # One concise activity line per pause batch — the user has
        # the dialog itself, and the parent agent doesn't need our
        # internal req_ids in its context.
        pause_line = (
            f"  {LogSymbols.T_TRUNK.value}  {LogSymbols.PAUSE.value} "
            f"awaiting approval ({len(req_ids)} tools)"
            if len(req_ids) > 1
            else f"  {LogSymbols.T_TRUNK.value}  {LogSymbols.PAUSE.value} awaiting approval"
        )
        self._log_line(pause_line)
        self._emit(
            AgentPausedEvent(
                agent_path=state.agent_path_id,
                count=len(req_ids),
            )
        )
        try:
            for req_id in req_ids:
                await coordinator.wait_resolved(req_id)
        finally:
            for req_id in req_ids:
                coordinator.cleanup(req_id)
        # ``run_id`` + ``session_id`` is enough for Agno to find the
        # paused run — but only when the agent has a ``db=`` so the
        # run was actually persisted. The pool wires ``InMemoryDb``
        # into every specialist for exactly this reason.
        return self._context.runnable.acontinue_run(
            run_id=run_id,
            session_id=session_id,
            requirements=reqs,
            stream=True,
        )

    async def _on_tool_started(self, event: agent_events.ToolCallStartedEvent) -> None:
        state = self.state
        te = event.tool
        tn = (te.tool_name or "tool") if te else "tool"
        ta = te.tool_args if te else {}
        args_preview = PREVIEWS.format_args(ta)
        args_full = PREVIEWS.format_full_args(ta)
        tc_id = getattr(te, "tool_call_id", None) if te else None
        state.current_tool = tn
        if tn == "visualize" and isinstance(ta, dict) and isinstance(ta.get("spec"), dict):
            await self._emit_visualizer_final_delta(ta["spec"])
        self._log_line(
            f"  {LogSymbols.T_TRUNK.value}  {LogSymbols.T_BRANCH.value} {tn}({args_full})"
        )
        self._emit(
            ToolStartedEvent(
                agent_path=state.agent_path_id,
                tool=tn,
                tool_call_id=tc_id,
                args=args_preview,
            )
        )

    async def _emit_visualizer_final_delta(self, spec: dict[str, Any]) -> None:
        """Visualizer final delta.

        The model finished streaming args, ``ta["spec"]`` now holds
        the complete parsed spec. Emit one closing ``final=True``
        delta so the FE can transition the card out of "streaming"
        mode, and log to the session event log so a reload replays
        it.

        The spec also goes to the activity log, which is how it
        reaches the agent that asked for it — see
        :data:`VIS_SPEC_PREFIX`. One line per ``visualize`` call; a
        visualizer that renders twice hands back both, and the caller
        picks.
        """
        state = self.state
        spec_json = json.dumps(spec)
        self._log_line(f"{VIS_SPEC_PREFIX}{spec_json}")
        final_event = VisualizationDeltaEvent(
            agent_path=state.agent_path_id,
            spec_id=state.vis_spec_id,
            spec_json=spec_json,
            final=True,
        )
        self._emit(final_event)
        appender = self._context.sinks.event_appender
        if appender is None:
            return
        try:
            # Use the PARENT (top-level) run_id, not the visualizer
            # sub-agent's own run_id — see
            # ``parent_top_run_id`` docstring on
            # :class:`SubAgentStreamState` and the ``get_chat_history``
            # splicing block in ``backend/server.py``.
            await appender(final_event, state.parent_top_run_id or state.current_run_id or "")
        except Exception as exc:  # noqa: BLE001 — a logging callback must not break the stream
            _logger.warning("visualization event log append failed: %s", exc)

    async def _on_tool_completed(self, event: agent_events.ToolCallCompletedEvent) -> None:
        state = self.state
        te = event.tool
        r = getattr(te, "result", None) if te else None
        tn = (te.tool_name if te else None) or state.current_tool or "tool"
        tc_id = getattr(te, "tool_call_id", None) if te else None
        result_preview = PREVIEWS.format_result(r)
        result_full = "" if r is None else str(r)
        self._log_line(
            f"  {LogSymbols.T_TRUNK.value}  {LogSymbols.T_TRUNK.value}  "
            f"{LogSymbols.T_LEAF.value} {result_full}"
        )
        self._emit(
            ToolCompletedEvent(
                agent_path=state.agent_path_id,
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
                agent_path=state.agent_path_id,
                tool=tn,
                tool_call_id=tc_id,
                result=err[:200],
                is_error=True,
            )
        )
        if state.current_tool == tn:
            state.current_tool = None

    async def _on_run_error(self, event: agent_events.RunErrorEvent) -> None:
        state = self.state
        err = str(getattr(event, "content", "") or getattr(event, "error", "?"))
        self._log_line(f"  {LogSymbols.T_TRUNK.value}  {LogSymbols.WARNING.value} RUN ERROR: {err}")
        self._emit(
            RunErrorEvent(
                agent_path=state.agent_path_id,
                error=err[:400],
            )
        )
        _stream_log.info(
            "subagent_stream: RunErrorEvent path=%s err=%s", self.state.agent_path_id, err[:300]
        )

    async def _on_run_completed(self, event: agent_events.RunCompletedEvent) -> None:
        # Last RunCompletedEvent of the run carries the full final
        # answer — keep the latest non-empty value as a fallback in
        # case the DB-backed lookup below comes up empty.
        state = self.state
        state.record_completion(getattr(event, "content", None))
        # Emit ``agent_completed`` with the run's metrics so the FE
        # can surface per-agent token totals. Agno's ``event.metrics``
        # is a ``Metrics`` object with the same fields the top-level
        # RunCompleted carries.
        mt = getattr(event, "metrics", None)
        self._emit(
            AgentCompletedEvent(
                agent_path=state.agent_path_id,
                is_error=False,
                input_tokens=int(getattr(mt, "input_tokens", 0) or 0) if mt else 0,
                output_tokens=int(getattr(mt, "output_tokens", 0) or 0) if mt else 0,
                reasoning_tokens=int(getattr(mt, "reasoning_tokens", 0) or 0) if mt else 0,
            )
        )

    async def _on_custom_event(self, event: agent_events.CustomEvent) -> None:
        """Tool-arg streaming (see ``_LoggingModel.process_response_stream``).

        When the visualizer sub-agent calls ``visualize({spec: ...})``,
        we get a ``tool_call_input_delta`` event on every model chunk
        with the accumulated arguments JSON. We extract the ``spec``
        sub-field with partial-json and forward as a
        ``visualization_delta`` so the FE renders live.

        ``tool_name`` and ``arguments_partial`` are not declared on
        Agno's ``CustomEvent`` — the model layer sets them on the
        instance — so they are read as attributes rather than fields.
        """
        if getattr(event, "event", "") != "tool_call_input_delta":
            return
        if getattr(event, "tool_name", "") != "visualize":
            return
        args_partial = str(getattr(event, "arguments_partial", "") or "")
        state = self.state
        if len(args_partial) <= state.vis_last_emitted_len:
            return
        now_s = time.monotonic()
        if not state.can_emit_vis_delta(now_s):
            return
        event_out = VisualizationDeltaEvent.from_partial_args(
            agent_path=state.agent_path_id,
            spec_id=state.vis_spec_id,
            args_partial=args_partial,
        )
        if event_out is None:
            return
        state.record_vis_emission(now_s, len(args_partial))
        self._emit(event_out)

    async def _on_run_content(self, event: agent_events.RunContentEvent) -> None:
        # Live progress preview only — final content comes from the
        # run's stored output after the stream ends.
        chunk = event.content or ""
        preview = self.state.append_content_delta(chunk)
        if preview is None:
            return
        self._emit(
            ContentPreviewEvent(
                agent_path=self.state.agent_path_id,
                text=preview,
            )
        )


__all__ = ["AGENT_EVENT_HANDLERS", "SubAgentStreamHandler"]
