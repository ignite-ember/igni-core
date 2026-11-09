"""Multiplex the team's Agno stream with the sub-agent coordinator.

Extracted from the previous ``backend.server_pause`` module — the
previous free ``stream_with_subagent_hitl`` function reached into
``backend._session``, ``backend._auto_resolved_requirements``, and
``backend._pending_requirements`` while spinning two background
tasks and pumping a raw 2-tuple asyncio.Queue. The class version
takes ``session`` / ``store`` / ``pause_handler`` / ``tracer`` as
constructor args (composition, not reach-back), and every queue
item is a typed :class:`MuxEvent`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from agno.run.agent import BaseAgentRunEvent, RunOutput
from agno.run.team import BaseTeamRunEvent, TeamRunOutput

from igni_core.backend.hitl_tracer import HITLTracer
from igni_core.backend.inline_think_splitter import InlineThinkSplitter
from igni_core.backend.pause_handler import PauseHandler
from igni_core.backend.pending_requirements_store import PendingRequirementsStore
from igni_core.backend.schemas_hitl import PausedRun
from igni_core.backend.schemas_pause import (
    MuxDone,
    MuxError,
    MuxEvent,
    SubagentDetails,
    SubagentPause,
    TeamStreamEvent,
)
from igni_core.core.activity import background
from igni_core.core.hitl.models import PendingHandle
from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator
from igni_core.protocol import messages as msg
from igni_core.protocol.agno_taxonomy import (
    RUN_COMPLETED_EVENTS,
    RUN_ERROR_EVENTS,
    RUN_PAUSED_EVENTS,
)
from igni_core.protocol.serializer import serialize_event

if TYPE_CHECKING:
    from igni_core.core.session import Session

_LLM_LOGGER = logging.getLogger("igni_core.llm_calls")


class HITLStreamMultiplexer:
    """Fan-in for team events + sub-agent HITL pauses.

    The team's stream and the sub-agent HITL coordinator are two
    independent producers of messages we need to forward to the FE:

    * ``team_stream`` is whatever Agno is currently driving — the
      initial ``team.arun`` call from ``run_message``, or a
      ``team.acontinue_run`` resumption from
      :meth:`HitlController.resolve_single`.
    * The coordinator wakes whenever a sub-agent (running inside a
      ``spawn_agent`` tool) hits a ``RunPausedEvent``. We have to
      surface that pause to the FE as a ``RunPaused`` message so the
      dialog appears.

    Both paths must run concurrently with the team stream; otherwise
    a sub-agent that pauses while the parent is still streaming
    events would have its requirement sitting in the coordinator
    forever with no one to forward it. Centralising this here means
    ``run_message`` AND :meth:`HitlController.resolve_single` both
    get the multiplexer — a previous version had it only in
    ``run_message`` so any sub-agent spawn that happened during a
    resumed run (parent paused for top-level Bash, user approved,
    parent resumed and then spawned an architect) silently dropped
    the architect's pauses.

    The team's own ``RunPausedEvent`` (parent pauses for its own
    tool) terminates this stream and is forwarded as ``RunPaused``;
    the FE then routes resolution back through
    :meth:`HitlController.resolve_single`, which calls this helper
    again with the resumed stream.
    """

    def __init__(
        self,
        session: Session,
        store: PendingRequirementsStore,
        pause_handler: PauseHandler,
        tracer: HITLTracer,
    ) -> None:
        """Bind the collaborators for one stream lifecycle.

        Instantiate one multiplexer per stream — the internal
        ``asyncio.Queue`` + drain tasks are per-call state, not
        shared across concurrent streams.
        """
        self._session = session
        self._store = store
        self._pause_handler = pause_handler
        self._tracer = tracer
        self._think_splitter = InlineThinkSplitter()
        # This stream's relay task and queue, while :meth:`stream` runs —
        # kept so a nested resume can stand the relay down
        # (:meth:`_hand_relay_to_nested`).
        self._sub_task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[MuxEvent] | None = None

    async def stream(self, team_stream: AsyncIterator[object]) -> AsyncIterator[msg.Message]:
        """Drive the multiplexer for one team stream lifecycle."""
        sub_hitl = self._session.sub_agent_hitl
        agno_queue: asyncio.Queue[MuxEvent] = asyncio.Queue()

        self._tracer.trace(f"_stream_mux: starting (coord_id={id(sub_hitl)})")

        # Hold the team-drain reference so the task isn't GC'd
        # mid-run (asyncio only weakly references background
        # tasks). It is not cancelled in ``finally`` — the team's stream
        # may still have a paused tool we want to drive to completion
        # via ``resolve_hitl``; it naturally exits when the team's
        # stream ends or errors. A cancelled run cancels it (below).
        # Counted before either task exists. The team task starts first,
        # and a tool call that asks for approval at once would otherwise
        # look for a relay that has not been scheduled yet, find none, and
        # be refused as "nobody to ask" — a race the model's thinking time
        # usually hides and a fast tool call does not.
        sub_hitl.open_relay()
        # Part of a run, which its session accounts for.
        team_task = background.daemon(self._drain_team(team_stream, agno_queue), name="hitl-team")
        sub_task = background.daemon(
            self._drain_subagent_hitl(sub_hitl, agno_queue), name="hitl-subagents"
        )
        self._sub_task, self._queue = sub_task, agno_queue

        try:
            while True:
                event = await agno_queue.get()
                if isinstance(event, MuxDone):
                    return
                if isinstance(event, MuxError):
                    # Re-raise the drained exception so the outer
                    # try/except path yields the right ``Error``
                    # message.
                    raise event.exc
                if isinstance(event, SubagentPause):
                    _LLM_LOGGER.info(
                        "subagent_hitl: yielding RunPaused to FE with %d req(s)",
                        len(event.entries),
                    )
                    yield event.run_paused()
                    continue
                if isinstance(event, SubagentDetails):
                    for update in event.updates:
                        yield msg.RequirementDetails(
                            requirement_id=update.requirement_id, details=update.details
                        )
                    continue
                # ``TeamStreamEvent`` — the common path.
                async for m in self._forward_team_event(event.event):
                    yield m
                if self._team_stream_should_stop(event.event):
                    return
        except asyncio.CancelledError:
            # The run was cancelled (``cancel_run``): the team's own run
            # stops too. Left draining, it went on running tools — and
            # pushing what they did — after the person had stopped it.
            # Never swallow cancellation — the ``run_message`` outer
            # ``except asyncio.CancelledError`` (in server.py)
            # needs to fire so the user sees "Run cancelled".
            team_task.cancel()
            raise
        except asyncio.TimeoutError:
            yield msg.Error(text="Request timed out — the model took too long to respond.")
        except Exception as e:
            # Narrowed catch: everything except CancelledError and
            # TimeoutError. Pattern-3 audit finding preserved as
            # comment — this is the last-resort surface so the FE
            # sees a message rather than a stranded stream.
            yield msg.Error(text=str(e))
        finally:
            sub_hitl.close_relay()
            sub_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sub_task
            # Retain the team_task reference until the ``finally``
            # returns so the GC comment above holds.
            del team_task

    # ── Drain producers ──────────────────────────────────────────────

    async def _drain_team(
        self,
        team_stream: AsyncIterator[object],
        queue: asyncio.Queue[MuxEvent],
    ) -> None:
        """Pull events off the team's Agno stream into the queue."""
        try:
            async for event in team_stream:
                await queue.put(TeamStreamEvent(event=event))
        except Exception as e:
            await queue.put(MuxError(exc=e))
        finally:
            await queue.put(MuxDone())

    async def _drain_subagent_hitl(
        self, sub_hitl: SubAgentHITLCoordinator, queue: asyncio.Queue[MuxEvent]
    ) -> None:
        """Pump sub-agent coordinator pauses into the queue.

        A coordinator failure goes on the queue as a :class:`MuxError`,
        so the stream ends in an ``Error`` the FE shows rather than a
        relay that silently stopped.
        """
        self._tracer.trace(f"_stream_mux: drain STARTED (coord_id={id(sub_hitl)})")
        try:
            while True:
                await sub_hitl.new_arrival.wait()
                entries = sub_hitl.list_new_pending()
                self._tracer.trace(f"_stream_mux: drain woke, {len(entries)} entries")
                if entries:
                    await queue.put(SubagentPause(entries=entries))
                    self._tracer.trace(
                        f"_stream_mux: drain enqueued {[h.requirement_id for h in entries]}"
                    )
                updates = sub_hitl.take_detail_updates()
                if updates:
                    await queue.put(SubagentDetails(updates=updates))
        except asyncio.CancelledError:
            self._tracer.trace("_stream_mux: drain cancelled")
            return
        except Exception as e:
            await queue.put(MuxError(exc=e))

    # ── Team event dispatch ────────────────────────────────────────

    async def _forward_team_event(self, event: object) -> AsyncIterator[msg.Message]:
        """Route one team event through the pause / cleanup / serialize path."""
        if isinstance(event, RUN_PAUSED_EVENTS):
            async for m in self._handle_paused(event):
                yield m
            return

        run_id = _run_id_of(event)
        run_finished = isinstance(event, RUN_COMPLETED_EVENTS + RUN_ERROR_EVENTS)
        if run_finished and run_id:
            # Sweep stale pending reqs for the finished run —
            # ``resolve_hitl_batch`` already pops the entries it
            # resolves; this catches the "user closed the UI
            # mid-pause and the run later wrapped up" path.
            self._store.sweep_run(run_id)
            self._think_splitter.end_run(run_id)

        serialized = serialize_event(event)
        if serialized is not None:
            for split_msg in self._think_splitter.split(serialized, run_id):
                yield split_msg

        if run_finished:
            # Drain post-run broadcasts (e.g. ``plan_submitted``
            # queued by ``exit_plan_mode``). The push fires AFTER
            # the run's content has flushed so the PlanCard lands
            # at the bottom of the agent's reply, not mid-stream.
            # ``run_id`` is stamped onto each payload so the FE can
            # key approve/dismiss RPCs by run_id. The bus contains
            # its subscribers' failures itself.
            self._session.broadcast_bus.drain_post_run(run_id=run_id)

    def _hand_relay_to_nested(self) -> None:
        """Stand this stream's relay down before a nested resume stream starts.

        The resume runs *inside* this stream's loop, so until it ends this
        loop reads nothing from its own queue. Its relay kept waiting on
        the shared coordinator all the same, and won the race for the next
        approval request about half the time — putting it in a queue
        nobody would read until the resumed run finished, which was itself
        waiting for that answer. A deadlock, and no dialog: found clicking
        through Bypass in the real UI, where every shell call is
        auto-resumed this way.

        Anything already taken is handed back unsurfaced, so the nested
        stream's relay picks it up.
        """
        coordinator = self._session.sub_agent_hitl
        if self._sub_task is not None:
            self._sub_task.cancel()
        queue = self._queue
        if queue is None:
            return
        handed_back = False
        unshown: list[PendingHandle] = []
        kept: list[MuxEvent] = []
        while not queue.empty():
            event = queue.get_nowait()
            if isinstance(event, SubagentPause):
                unshown.extend(event.entries)
                handed_back = True
            elif isinstance(event, SubagentDetails):
                for update in event.updates:
                    coordinator.update_details(update.requirement_id, update.details)
                handed_back = True
            else:
                kept.append(event)
        for event in kept:
            queue.put_nowait(event)
        if handed_back:
            coordinator.hand_back(unshown)

    async def _handle_paused(self, event: PausedRun) -> AsyncIterator[msg.Message]:
        """Route a paused team event through the evaluator and
        either resume or forward to the FE."""
        result = self._pause_handler.handle(event)
        for pause_msg in result.messages:
            yield pause_msg
        if result.messages:
            # Mixed pause: some reqs still need the user.
            # Stash the auto-resolved ones so
            # ``resolve_hitl_batch`` can merge them into the
            # eventual ``acontinue_run`` call.
            if result.auto_resolved and result.run_id:
                self._store.stash_auto_resolved(result.run_id, result.auto_resolved)
            return
        if result.auto_resolved and result.run_id:
            # Every req was decided by the evaluator. Resume the
            # team immediately without ever bothering the FE — this
            # is the plan-mode / acceptEdits / bypass / deny-rule
            # short-circuit.
            team = self._session.main_team
            _LLM_LOGGER.info(
                "auto-resuming run_id=%s with %d evaluator-resolved req(s)",
                result.run_id,
                len(result.auto_resolved),
            )
            self._hand_relay_to_nested()
            resume_mux = HITLStreamMultiplexer(
                session=self._session,
                store=self._store,
                pause_handler=self._pause_handler,
                tracer=self._tracer,
            )
            async for proto in resume_mux.stream(
                team.acontinue_run(
                    run_id=result.run_id,
                    session_id=self._session.session_id,
                    requirements=result.auto_resolved,
                    stream=True,
                    stream_events=True,
                )
            ):
                yield proto
            return
        # No requirements at all — defensive; shouldn't normally
        # happen but don't strand the stream.
        return

    def _team_stream_should_stop(self, event: object) -> bool:
        """The paused-event branch is expected to terminate the
        outer loop after the pause is forwarded/resumed. Everything
        else keeps the stream going."""
        return isinstance(event, RUN_PAUSED_EVENTS)


def _run_id_of(event: object) -> str | None:
    """The run an Agno stream event belongs to, if it names one."""
    if isinstance(event, (BaseAgentRunEvent, BaseTeamRunEvent, RunOutput, TeamRunOutput)):
        return event.run_id
    return None
