"""The base every Agno stream handler shares.

Owns everything :class:`~igni_core.core.tools.orchestrate_agent_stream.SubAgentStreamHandler`
and :class:`~igni_core.core.tools.orchestrate_team_stream.TeamStreamHandler`
have in common:

* the outer ``while stream is not None`` loop that handles
  pause/resume follow-up streams;
* ``run_id`` / ``session_id`` latching and the cancellation-registry
  register/discard around the loop;
* the read-back of the run's final answer, and the ``agent_completed``
  the frontend needs to stop a card spinning;
* one-line ``_emit`` / ``_log_line`` helpers so subclasses read
  clean.

The collaborators arrive as one :class:`StreamContext` and the run's
memory as one :class:`StreamState`, so the base asks for what it
needs rather than probing for it: ``state.current_run_id`` is a
field, not a ``getattr(state, "current_run_id", None)`` that answers
``None`` for a state that simply forgot.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Generic, TypeVar

from igni_core.core.tools.orchestrate_events import AgentCompletedEvent, OrchestrateEvent
from igni_core.core.tools.orchestrate_stream_context import StreamContext
from igni_core.core.tools.orchestrate_stream_finalize import RunFinalizer
from igni_core.core.tools.orchestrate_stream_state import StreamState

_stream_log = logging.getLogger("igni_core.llm_calls")

_TState = TypeVar("_TState", bound=StreamState)


def active_requirements(event: Any) -> list[Any]:
    """The requirements a paused run is still waiting on.

    Agno derives this from the run's requirements, each of which knows
    whether it has been resolved. A requirement that cannot answer
    leaves the run with nothing to wait for, and the run carries on
    without a bridge.
    """
    try:
        return list(event.active_requirements)
    except AttributeError:
        return []


def handler_name(table: Mapping[type, str], event: object) -> str | None:
    """The name of the handler that answers ``event``, if any.

    ``table`` maps an Agno event class to a method name on the
    handler. The lookup walks the event's method resolution order, so
    a class that derives from a handled event is answered by its
    parent's handler rather than falling through.

    The walk is why no table may key on a shared base class: Agno's
    agent events and team events both descend from
    ``BaseRunOutputEvent``, and a key on it would make a team's
    coordinator look like a worker.
    """
    cls = getattr(event, "__class__", type(event))
    for klass in getattr(cls, "__mro__", (cls,)):
        name = table.get(klass)
        if name is not None:
            return name
    return None


class BaseStreamHandler(Generic[_TState]):
    """Abstract driver for one Agno agent-or-team stream.

    Subclasses supply the state model and the per-event handling
    (``_handle`` returns a follow-up async iterator on pause+resume,
    else ``None``). Everything else — the outer loop, the
    cancellation registry hook-up, the final read, the emit helper —
    lives here so the two concrete subclasses stay focused on event
    dispatch.
    """

    def __init__(self, context: StreamContext, state: _TState) -> None:
        self._context = context
        self.state = state

    # ── Subclass hooks ─────────────────────────────────────────────

    async def _handle(self, event: Any) -> Any:
        """Dispatch one Agno event. Returns a follow-up async
        iterator when a resume was requested (pause+continue),
        else ``None``.

        Concrete subclasses override.
        """
        raise NotImplementedError

    def _shape_final(self, final: str) -> str:
        """Post-loop transformation of the raw final answer string.

        Default: return unchanged. The sub-agent handler overrides so
        a visualizer returns a short summary line instead of the raw
        spec JSON.
        """
        return final

    def _agent_completed_path(self) -> str:
        """The ``agent_path`` field on the ``agent_completed`` event.

        Subclasses override for teams — the sub-agent path here isn't
        correct for the team's own finalization emit.
        """
        path = self._context.agent_path
        return path[0] if path else "root"

    # ── Public entry point ─────────────────────────────────────────

    async def run(self) -> tuple[str, list[str]]:
        """Drive the stream to completion. Returns ``(response, log)``
        — the sub-agent's final text and the activity log the parent
        agent recaps.
        """
        registry = self._context.sinks.subagent_registry
        coordinator = self._context.sinks.hitl_coordinator
        try:
            stream = self._first_stream()
            while stream is not None:
                next_stream = None
                async for event in stream:
                    if next_stream is not None:
                        # Let the paused run's stream end on its own.
                        # Breaking out left Agno to close it later as a
                        # cancelled run — which clears the pause on the
                        # very run object the continuation reads, so the
                        # agent asked for the same approval again.
                        continue
                    follow_up = await self._handle(event)
                    if follow_up is not None:
                        next_stream = follow_up
                stream = next_stream
        finally:
            run_id = self.state.current_run_id
            if run_id:
                registry.discard(run_id)
                # Whatever waited above this run has its answer now (or
                # never will): nothing hangs from it any more.
                if coordinator is not None:
                    coordinator.forget_spawn(run_id)

        self._ensure_agent_completed_emitted()
        final = await self._finalizer().final_content()
        return self._shape_final(final), self.state.log

    def _first_stream(self) -> Any:
        """The stream the run starts with: a fresh run of the task."""
        return self._context.runnable.arun(self._context.task, stream=True)

    # ── Shared helpers ─────────────────────────────────────────────

    def _log_line(self, line: str) -> None:
        """Append one line to the parent-recap activity log. Not
        FE-facing — the FE gets structured events via ``_emit``."""
        self.state.log.append(line)

    def _emit(self, event: OrchestrateEvent) -> None:
        """Deliver one structured event to the progress callback.

        The event travels as the model it is — the frontend's wire
        shape is produced once, at the push boundary. The card id is
        stamped here so every event of one run lands on the same card
        across interleaved info items, a page refresh, and concurrent
        spawns.
        """
        callback = self._context.sinks.on_progress
        if callback is None:
            return
        card_id = self._context.card_id
        if card_id:
            event.card_id = card_id
        try:
            callback(event)
        except Exception as exc:
            _stream_log.warning("progress callback failed: %s", exc)

    def _latch_run_ids(self, event: object) -> None:
        """Latch the run ids off the event, and register the run with
        the cancellation registry when a fresh one lands."""
        if self.state.latch_ids(event):
            run_id = self.state.current_run_id
            if run_id:
                # ``BackendServer.cancel_run`` iterates the registry
                # to reach every in-flight sub-agent. Without this
                # add, a stuck specialist ignores the top-level ESC.
                self._context.sinks.subagent_registry.register(run_id)

    def _ensure_agent_completed_emitted(self) -> None:
        """Say the run is done, when nothing else has.

        Agno's specialist ``arun`` doesn't yield ``RunCompletedEvent``
        unless ``stream_events=True`` — which is deliberately kept off
        so the wire carries no lifecycle noise. The frontend stops the
        sub-agent's card on ``agent_completed``, so a stream that ends
        without one says it here.

        A team does not: each of its members reports its own
        completion, and a blanket team completion would say the whole
        team finished when one member did. The state says which kind
        of stream this is.
        """
        if not self.state.emits_agent_completed:
            return
        if self.state.agent_completed_emitted:
            return
        self._emit(AgentCompletedEvent(agent_path=self._agent_completed_path()))
        self.state.agent_completed_emitted = True

    def _finalizer(self) -> RunFinalizer:
        """The reader that fetches the run's final answer back."""
        return RunFinalizer(self._context.runnable, self.state, self._context.agent_path)


__all__ = ["BaseStreamHandler", "active_requirements", "handler_name"]
