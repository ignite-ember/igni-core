"""The main run engine — user-message dispatch + streaming.

Replaces the old free-function :mod:`igni_core.backend.server_run`
module. That module was three functions taking ``BackendServer``
as their first arg and reaching into ~17 private attributes.
This class owns all of that state as instance attributes and
delegates the pre-run pipeline concerns to four collaborators:

* :class:`PromptBuilder` — mentions + media + URL + interrupted
  summary composition.
* :class:`PendingMessageJournal` — pre-persist + drop-on-continue
  + mark-completed.
* :class:`RunHookGate` — UserPromptSubmit + Stop hook fires.
* :class:`StreamEventDispatcher` — per-event side-effects
  (token latch + tool-boundary checkpoints).

Plus one class for the httpx close/replace:

* :class:`ModelHttpClientManager` — replaces the module-level
  ``_HTTP_CLIENT_LIMITS`` + free ``close_model_http_client``.

Public API (used by ``BackendServer``):

* :meth:`run_message` — serialises submits on :attr:`run_lock`,
  tracks the running task and drives the locked run body.
* :meth:`is_processing` — bool for the FE's ``get_processing``
  RPC — forwarded via ``BackendServer.processing``.
* :meth:`set_interrupted_summary` — writer used by
  :meth:`LifecycleController.detect_interrupted_run`. Takes a
  typed :class:`InterruptedRunSummary` so the summary text +
  drop ids travel as one value.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar

from agno.agent import Agent
from agno.team import Team

from igni_core.backend.model_http_client_manager import ModelHttpClientManager
from igni_core.backend.pending_message_journal import PendingMessageJournal
from igni_core.backend.prompt_builder import PromptBuilder
from igni_core.backend.run_hook_gate import RunHookGate
from igni_core.backend.schemas_hitl import StreamFactory
from igni_core.backend.schemas_lifecycle import InterruptedRunSummary
from igni_core.backend.schemas_run import CancelAgentRunResult, RunPhase
from igni_core.backend.session_checkpointer import SessionCheckpointer
from igni_core.backend.stream_event_dispatcher import StreamEventDispatcher
from igni_core.core.activity import BusyKind, background
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.run_context import run_session
from igni_core.core.session.pending_messages import InterruptedReason
from igni_core.core.session.queued_messages import DurableMessageQueue
from igni_core.core.session.schemas import StopHookPayload
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.utils.media_schemas import MediaAttachments
from igni_core.protocol import messages as msg

if TYPE_CHECKING:
    from igni_core.core.session import Session
    from igni_core.core.session.pending_messages import PendingMessageStore


logger = logging.getLogger(__name__)


_T = TypeVar("_T")

#: What a cancelled run's stream says before it ends: shown to the person,
#: never given to the model.
CANCELLED_BY_USER = "Run cancelled by user."


class RunCancelledError(RuntimeError):
    """The person cancelled the run this work was."""


class RunController:
    """Owns the run lifecycle for a single ``BackendServer``.

    One instance per ``BackendServer`` — reused across every
    ``run_message`` call. Sole owner of the run phase, the run lock,
    the running task and the interrupted-run handoff; drives the
    pre-run pipeline + streaming loop.

    ``stream_factory`` wraps the team stream with sub-agent HITL
    multiplexing (:meth:`HitlController.stream_with_subagent` in
    production). ``message_queue`` is the durable queue settled
    after each run. ``supervisor`` is the supervisor the backend
    process shares — its foreground slot is what a cancel kills.
    ``gate`` is the run gate of the backend process: no run starts
    while it is closed.
    """

    #: The pending message of a run that paused for a person: the run has
    #: not finished, so the row stays pending until its resume ends it.
    _paused_pending_id: str | None = None
    #: Drops the approvals the session's runs left waiting; returns how
    #: many. A cancel calls it, so no later answer continues a run the
    #: person stopped (:meth:`HitlController.drop_waiting`).
    _drop_approvals: Callable[[], int] | None = None
    #: Drops them for a new message, and keeps each paused run's turn in
    #: the history with its calls declined (:meth:`HitlController.abandon_waiting`).
    _abandon_approvals: Callable[[], Awaitable[list[str]]] | None = None
    #: Sends a message to every client of the session: where the stream of
    #: a run a cancel ended paused gets its end (:meth:`stream_to`).
    _send: Callable[[msg.Message], Awaitable[None]] | None = None

    def __init__(
        self,
        session: Session,
        pending_store: PendingMessageStore,
        message_queue: DurableMessageQueue | None,
        stream_factory: StreamFactory,
        supervisor: ProcessSupervisor,
        gate: RunGate,
        http_client_manager: ModelHttpClientManager | None = None,
        drop_approvals: Callable[[], int] | None = None,
        abandon_approvals: Callable[[], Awaitable[list[str]]] | None = None,
    ) -> None:
        self._session = session
        self._drop_approvals = drop_approvals
        self._abandon_approvals = abandon_approvals
        self._message_queue = message_queue
        self._stream_factory = stream_factory
        self._supervisor = supervisor
        self._gate = gate
        # Asked per write: New chat and a sidebar switch change the id.
        self._pending_journal = PendingMessageJournal(pending_store, lambda: session.session_id)
        self._http_client_manager = http_client_manager or ModelHttpClientManager()
        self._prompt_builder = PromptBuilder(session)
        self._hook_gate = RunHookGate(lambda: session.hook_executor, lambda: session.session_id)
        self._dispatcher = StreamEventDispatcher(session, self.checkpoint)
        self._phase = RunPhase.idle
        self._interrupted_summary: InterruptedRunSummary | None = None
        # Serialisation + cancellation state — single owner (Pattern 1).
        self._run_lock = asyncio.Lock()
        # Every task running one of the session's runs: a turn, a resumed
        # run, a scheduled task run now. More than one at a time when a
        # message starts a turn while a resumed run goes on — one slot lost
        # the resumed run, and ``cancel_run`` found nothing to stop.
        self._run_tasks: set[asyncio.Task] = set()
        # Resumes that pass through a run already going (a sub-agent's
        # answer): not runs of their own, but a cancel ends them too.
        self._passengers: set[asyncio.Task] = set()
        # The current run's stream ended paused for a person: the run is
        # only closing, and its answer resumes it (:meth:`continue_run`).
        self._stream_ended_paused = False
        # A streamed turn — a new message's or a resumed one's — holds the run.
        self._turn_working = False
        #: The chat the latest run belongs to; a resumed run is still it.
        self._run_session_id = ""
        # The tasks ``cancel_run`` cancelled, so the work they were running
        # can tell the person's cancel from any other.
        self._cancelled_by_person: set[asyncio.Task] = set()
        # The pending-message id stamped by :meth:`_run_locked`
        # before streaming starts. Held on the instance so the
        # sync :meth:`cancel_run` can mark the row as interrupted
        # without an async hop — see ``_stamp_interrupted``.
        self._pending_id_for_cancel: str | None = None

    # ── Public API used by BackendServer ────────────────────────────

    @property
    def pending_journal(self) -> PendingMessageJournal:
        """Underlying pending-message journal (kept accessible for
        ``get_pending_messages`` + ``detect_interrupted_run`` which
        still call the low-level store methods directly)."""
        return self._pending_journal

    @property
    def phase(self) -> RunPhase:
        """Current lifecycle phase — inspected by
        ``BackendServer.processing``."""
        return self._phase

    @property
    def run_lock(self) -> asyncio.Lock:
        """The outer serialization lock guarding every ``run_message``
        invocation."""
        return self._run_lock

    @property
    def current_run_task(self) -> asyncio.Task | None:
        """A task running one of the session's runs, or ``None`` when idle."""
        return next(iter(self._run_tasks), None)

    def is_processing(self) -> bool:
        """True while any run of the session is going.

        A turn that ended while a resumed run still goes on leaves the
        phase ``done``; the session is busy all the same.
        """
        return self._phase.is_active() or bool(self._run_tasks)

    @property
    def turn_in_progress(self) -> bool:
        """A streamed turn is working — not paused on a person, nor closing a pause."""
        return self._turn_working and not self._stream_ended_paused

    def _begin_run(self) -> asyncio.Task | None:
        """The current task is one of the session's runs until :meth:`_end_run`."""
        task = asyncio.current_task()
        if task is not None:
            self._run_tasks.add(task)
        return task

    def _end_run(self, task: asyncio.Task | None) -> None:
        """``task``'s run is over; the phase settles once no other run goes on."""
        if task is not None:
            self._run_tasks.discard(task)
            self._cancelled_by_person.discard(task)
        if not self._run_tasks and self._phase.is_active():
            self._transition_to(RunPhase.done)

    def set_interrupted_summary(self, summary: InterruptedRunSummary | None) -> None:
        """Route for ``detect_interrupted_run`` to hand off the
        one-shot summary + the pending ids that should be dropped
        on the next ``run_message``.

        The pair ``(summary_text, pending_ids_to_drop)`` is one
        semantic value — a typed
        :class:`InterruptedRunSummary` — rather than two
        positional args. The journal's drop queue is populated
        as a side-effect so the next :meth:`_run_locked` call
        drains it.
        """
        self._interrupted_summary = summary
        if summary is not None:
            self._pending_journal.queue_drops(summary.pending_ids_to_drop)

    def _consume_interrupted_note(self, text: str | None) -> str | None:
        """Render the one-shot interrupted-run note for *this* delivery, or
        ``None`` when the previous shutdown was clean.

        The recap is built at startup, before anyone has spoken; it is read by
        the model here, where ``text`` is the message the user just sent. Which
        of those two situations it is decides the closing instruction, so the
        choice is made at the point that knows — see
        :meth:`InterruptedRunSummary.render`. It previously always claimed the
        user had not sent a new message, one line above the message they had.

        Consuming is part of the contract: the summary is one-shot, so it is
        cleared whether or not a note comes back out.
        """
        summary, self._interrupted_summary = self._interrupted_summary, None
        if summary is None:
            return None
        return summary.render(awaiting_user=not (text or "").strip())

    # ── State transitions ───────────────────────────────────────────

    def _transition_to(self, phase: RunPhase) -> None:
        """Single writer for :attr:`_phase`. Every state change goes
        through this so a future audit for stray ``self._phase = X``
        assignments has one method to grep and one place to add
        guards / logging."""
        self._phase = phase

    # ── Run entry / cancel (moved from BackendServer) ────────────────

    async def run_message(
        self, text: str, media: MediaAttachments | None
    ) -> AsyncGenerator[msg.Message, None]:
        """Streaming entry point — serialises concurrent submits and
        tracks the running task.

        Moved from ``BackendServer.run_message`` so the outer lock +
        current-task binding + Cancelled-error handling live with
        the pipeline that owns them. server.py forwards
        ``run_message`` to this method 1:1.
        """
        async with self._run_lock:
            self._stream_ended_paused = False
            self._turn_working = True
            # The chat this run is, for everything it sends while it runs.
            self._run_session_id = self._session.session_id
            # A cancelled run reaches none of ``_run_locked``'s terminal
            # transitions (``CancelledError`` is a BaseException): the run's
            # end settles the phase (:meth:`_relay`), or ``get_processing``
            # said busy for the rest of the process's life.
            try:
                with run_session(self._run_session_id):
                    relay = self._relay(self._run_locked(text, media), self._run_tasks)
                    async with contextlib.aclosing(relay) as frames:
                        async for proto in frames:
                            yield proto
            finally:
                self._turn_working = False

    async def continue_run(
        self, stream: AsyncIterator[msg.Message]
    ) -> AsyncGenerator[msg.Message, None]:
        """A paused run, resumed once its approvals are answered, as this
        session's run: ``cancel_run`` reaches it and ``get_processing``
        reports it, as for the run that paused.

        Under the run lock, as every turn is: a new message waits for it
        (or is queued into it) instead of starting a second run beside it,
        blind to this one. Only a sub-agent's approval, answered while its
        parent run still holds the lock, passes through: the parent is the
        session's run already.
        """
        # The chat of the run that paused, though New chat may have moved
        # the session on since: what it streams is that chat's.
        with run_session(self._run_session_id or self._session.session_id):
            if self._run_tasks and not self._stream_ended_paused:
                relay = self._relay(stream, self._passengers)
                async with contextlib.aclosing(relay) as frames:
                    async for proto in frames:
                        yield proto
                return
            # A run that paused may still be closing (its checkpoint, its model
            # client) when a quick answer arrives: the lock waits for it to let go.
            async with self._run_lock:
                self._stream_ended_paused = False
                self._turn_working = True
                try:
                    self._transition_to(RunPhase.streaming)
                    relay = self._relay(self._continued(stream), self._run_tasks)
                    async with contextlib.aclosing(relay) as frames:
                        async for proto in frames:
                            yield proto
                finally:
                    self._turn_working = False

    async def _continued(
        self, stream: AsyncIterator[msg.Message]
    ) -> AsyncGenerator[msg.Message, None]:
        """The resumed run's stream, closed as the run that paused would be."""
        events_before = self._session.event_log_store.seq
        pending_id = self._paused_pending_id
        reply: list[str] = []
        # Ended only once the resumed run says so: an answer that resumed
        # nothing (an unknown requirement) or a run that paused again has
        # no reply for Stop to judge.
        ended = False
        try:
            # The chat of the run that paused, though New chat may have
            # moved the session on while the approval waited.
            with run_session(self._run_session_id or self._session.session_id):
                async for proto in stream:
                    yield proto
                    if isinstance(proto, msg.ContentDelta):
                        reply.append(proto.text)
                    ended = not self._paused_after(proto, not ended)
            self._stream_ended_paused = not ended
            if ended and pending_id is not None:
                # The run that paused has finished: its message is done.
                self._paused_pending_id = None
                self._pending_id_for_cancel = None
                await self._pending_journal.mark_completed(pending_id)
            if ended:
                # The run ended: Stop sees its reply, and a block sends the
                # agent back, as at the end of a run that never paused.
                async for proto in self._stop_or_retry(self._session.main_team, "".join(reply)):
                    yield proto
        except asyncio.CancelledError:
            # ``cancel_run`` stamped the message as cancelled.
            self._paused_pending_id = None
            yield self.cancelled_info()
        except Exception as exc:
            if pending_id is not None and self._paused_pending_id == pending_id:
                self._paused_pending_id = None
                self._pending_id_for_cancel = None
                await self._pending_journal.mark_interrupted(
                    pending_id, "errored", last_error=str(exc)
                )
            raise
        finally:
            await self._keep_event_log(events_before)

    async def _relay(
        self, body: AsyncIterator[msg.Message], tasks: set[asyncio.Task]
    ) -> AsyncGenerator[msg.Message, None]:
        """Run ``body`` in a task of its own, kept in ``tasks``, and yield what it streams.

        That task is what ``cancel_run`` cancels, so a cancel lands inside
        the run. Cancelling the task that sends the frames to the windows
        often landed on a send instead, where the run never heard it: its
        team went on and the session stayed busy for every window (session
        6fa3e09e). A consumer that stops reading stops the run.
        """
        frames: asyncio.Queue[msg.Message | None] = asyncio.Queue()

        async def pump() -> None:
            async for proto in body:
                frames.put_nowait(proto)

        task = background.daemon(pump(), name="session-run")
        # A callback, not a ``finally``: a task cancelled before its first
        # step never runs its body at all.
        task.add_done_callback(lambda _done: frames.put_nowait(None))
        tasks.add(task)
        try:
            while (proto := await frames.get()) is not None:
                yield proto
            await task
        except asyncio.CancelledError:
            if not (task.cancelled() and task in self._cancelled_by_person):
                raise
            yield self.cancelled_info()
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if tasks is self._run_tasks:
                self._end_run(task)
            else:
                tasks.discard(task)
                self._cancelled_by_person.discard(task)

    async def _close_abandoned_pause(self) -> None:
        """A new message while a run stands paused: the person moved on from
        that run, so its message is closed as completed, as it was before a
        pause kept it open.

        Its approvals go too, as a cancel drops them (:meth:`_end_pause`):
        left waiting, answering one continued the abandoned run and ran
        its tool in the middle of the new conversation. That includes the
        ones a previous backend left, which no paused message here names.
        Each paused run's turn stays in the history, its waiting calls
        declined, so the new message is read after it, and every window
        withdraws their dialogs.
        """
        if self._abandon_approvals is not None:
            withdrawn = await self._abandon_approvals()
            await self._withdraw(withdrawn)
            dropped = len(withdrawn)
        else:
            dropped = self._drop_approvals() if self._drop_approvals is not None else 0
        if dropped:
            logger.info("A new message dropped %d waiting approval(s)", dropped)
        pending_id = self._paused_pending_id
        if pending_id is None:
            return
        self._paused_pending_id = None
        if self._pending_id_for_cancel == pending_id:
            self._pending_id_for_cancel = None
        await self._pending_journal.mark_completed(pending_id)

    async def _withdraw(self, requirement_ids: list[str]) -> None:
        """Tell every window of the paused run's chat that these approvals are gone."""
        send = self._send
        if send is None:
            return
        chat = self._run_session_id or self._session.session_id
        for requirement_id in requirement_ids:
            await send(msg.RequirementResolved(requirement_id=requirement_id, session_id=chat))

    async def end_paused_turn(self) -> None:
        """The session moves off its chat (``/clear``, ``/fork``): a turn paused
        on the person ends there as a new message ends it; a working turn goes on."""
        if self._run_lock.locked() and not self._stream_ended_paused:
            return
        async with self._run_lock:
            await self._close_abandoned_pause()

    async def run_unstreamed(self, work: Callable[[], Awaitable[_T]]) -> _T:
        """Work on the session's team outside a streamed turn — a scheduled
        task run now — as the session's run.

        One at a time with the turns (the run lock), reported by
        ``get_processing``, stopped by ``cancel_run``: a cancel ends it
        with :class:`RunCancelledError` for whoever asked for the work.
        """
        async with self._run_lock:
            task = self._begin_run()
            self._transition_to(RunPhase.streaming)
            try:
                with run_session(self._session.session_id):
                    return await work()
            except asyncio.CancelledError:
                if task is None or task not in self._cancelled_by_person:
                    raise
                task.uncancel()
                raise RunCancelledError(CANCELLED_BY_USER) from None
            finally:
                self._end_run(task)

    def cancel_run(self) -> bool:
        """Cancel the currently running agent and kill any
        foreground process.

        Moved from ``BackendServer.cancel_run``. Cancels the outer
        task tracked by :attr:`current_run_task`; the
        ``CancelledError`` unwinds through ``team.arun``'s generator,
        which is what stops the Agno run. (It also used to call
        ``Agent.cancel_run(team.run_id)``, but Agno's ``Agent`` has no
        ``run_id`` attribute, so that branch never fired.)

        Also stamps the pending user message as an explicit
        interrupted run (``reason='cancelled'``) so the FE has a
        durable record on next render — instead of inferring
        interruption from a dangling pending row + the legacy
        ``RunStatus.running`` heuristic. The stamp is fire-and-
        forget so the cancel response doesn't wait on the disk.

        A run paused on an approval has no task left to cancel, and is
        ended all the same: its approvals are dropped, so answering one
        later runs nothing, and the cancel answers ``True``. It used to
        answer ``False`` and leave the approval waiting — the next
        window was offered it, and answering it ran the tool.

        Every run of the session goes, not only the latest: a turn started
        while a resumed run went on took the one slot there was, and once
        it ended the resumed run was out of reach — it ran on for minutes
        while every Escape answered ``False``.
        """
        self._cancel_own_foreground()

        self._stamp_interrupted("cancelled", last_error=None)
        ended_a_pause = self._end_pause()

        live = [t for t in (*self._run_tasks, *self._passengers) if not t.done()]
        for task in live:
            logger.info("Cancelling run task %s", task.get_name())
            self._cancelled_by_person.add(task)
            task.cancel()
        if live:
            return True
        if ended_a_pause:
            self._end_cancelled_pause()
        return ended_a_pause

    @staticmethod
    def cancelled_info() -> msg.Info:
        """What a cancelled run's stream says last, before its ``StreamEnd``."""
        return msg.Info(text=CANCELLED_BY_USER)

    def stream_to(self, send: Callable[[msg.Message], Awaitable[None]]) -> None:
        """Send what this session streams outside a run through ``send``:
        the runtime's transport, which every client of the session reads."""
        self._send = send

    def _end_cancelled_pause(self) -> None:
        """The stream of a run a cancel ended paused ends as a cancelled
        running run's does: an ``info`` saying so, then a ``StreamEnd``.

        The paused run streamed its last frame long ago, so a client told
        ``cancelled: true`` waited for a ``stream_end`` that never came.
        Sent after the cancel answers, as a running run's are once its
        task unwinds.
        """
        send = self._send
        if send is None:
            return
        # The chat of the run that paused, though New chat may have moved
        # the session on while the approval waited (as :meth:`continue_run`).
        chat = self._run_session_id or self._session.session_id

        async def end() -> None:
            with run_session(chat):
                await send(self.cancelled_info())
                await send(msg.StreamEnd())

        background.start_if_running(
            end(),
            holder="run-cancel",
            reason="ending the stream of a cancelled pause",
            kind=BusyKind.POST_RUN,
        )

    def _end_pause(self) -> bool:
        """End the run that stands paused for a person; ``True`` when one did.

        Its approvals go — the ones this backend asked and the ones a
        previous backend left — and its message, already stamped
        cancelled, is no longer the paused run's: a later message must
        not close it as completed (:meth:`_close_abandoned_pause`).
        """
        paused = self._paused_pending_id is not None
        if paused:
            self._paused_pending_id = None
            self._pending_id_for_cancel = None
        dropped = self._drop_approvals() if self._drop_approvals is not None else 0
        if dropped:
            logger.info("Cancel dropped %d waiting approval(s)", dropped)
        return paused or dropped > 0

    def _cancel_own_foreground(self) -> None:
        """Kill the foreground shell — but only if this session
        could plausibly own it.

        The supervisor is process-wide and one BE process
        hosts N sessions (see :mod:`session_pool`), so the foreground
        slot is shared by every window. Cancelling an *idle* session
        used to reach into that shared slot and kill whatever another
        window was running in the foreground — someone's ``pytest``,
        ``npm run build`` or ``alembic upgrade``, in a different
        repository, with no indication why.

        A foreground shell only exists while the run that spawned it
        is blocked on it, so a session that is not streaming cannot
        be the owner and must not touch the slot. That closes the
        idle-window case; the both-sessions-streaming case still
        mis-attributes, and cannot be fixed from here — the
        supervisor would have to stamp an owning session id on the
        foreground slot (``core/tools/process_supervisor.py``).
        """
        if self._phase is not RunPhase.streaming and not self._run_tasks:
            logger.debug(
                "cancel: session is %s, not streaming — leaving the shared foreground slot alone",
                self._phase,
            )
            return
        if self._supervisor.cancel_foreground():
            logger.info("Killed foreground process on cancel")

    def _stamp_interrupted(
        self,
        reason: InterruptedReason,
        last_error: str | None,
    ) -> None:
        """Schedule a fire-and-forget mark-interrupted on the pending row.

        No-op if no pending row is currently bound to a run
        (``_pending_id_for_cancel is None``) — that means the run
        finished cleanly or hasn't started yet. The cancel RPC is
        safe to call from either of those states.

        The task is intentionally un-awaited: the WS cancel handler
        returns to the client immediately, and the SQLite write is
        small enough that a dropped task (e.g. process exit
        immediately after cancel) is harmless — at worst the next
        boot falls back to the legacy ``RunStatus.running``
        heuristic.
        """
        pending_id = self._pending_id_for_cancel
        if pending_id is None:
            return
        background.start(
            self._pending_journal.mark_interrupted(pending_id, reason, last_error),
            holder="run",
            reason="recording an interrupted run",
            name=f"mark_interrupted:{reason}:{pending_id[:8]}",
        )

    def cancel_agent_run(self, run_id: str) -> CancelAgentRunResult:
        """Cancel a specific sub-agent run by its Agno ``run_id``.

        Moved from ``BackendServer.cancel_agent_run``. Used by the
        team-progress UI when the user wants to stop one specialist
        mid-broadcast without killing the whole team.
        """
        if not run_id:
            return CancelAgentRunResult(ok=False, error="missing run_id")
        try:
            running = Agent.cancel_run(run_id)
        except Exception as exc:
            logger.warning("cancel_agent_run failed: %s", exc)
            return CancelAgentRunResult(ok=False, error=str(exc))
        if not running:
            return CancelAgentRunResult(ok=False, error=f"no run {run_id!r} is running")
        logger.info("Cancelled sub-agent run %s", run_id)
        return CancelAgentRunResult(ok=True)

    # ── Checkpointing (moved from BackendServer) ────────────────────

    async def checkpoint(self, team: Team) -> None:
        """Force Agno to persist the in-flight session — one-shot.

        A method (not a static) so ``patch.object`` on the
        controller intercepts the call from :meth:`periodic_checkpoint`.
        """
        await SessionCheckpointer(team).snapshot()

    async def periodic_checkpoint(self, team: Team, interval: float = 3.0) -> None:
        """Loop that snapshots the session every ``interval`` seconds.

        Each tick calls :meth:`checkpoint`.
        """
        checkpointer = SessionCheckpointer(team)
        await checkpointer.run_forever(
            interval=interval,
            checkpoint_hook=self.checkpoint,
        )

    # ── Stream outcome ──────────────────────────────────────────────

    @staticmethod
    def _failure_text(proto: msg.Message) -> str | None:
        """The failure this streamed message reports, or ``None``.

        The ``except Exception`` branch of :meth:`_run_locked` used to
        be the only thing that stamped a run interrupted, and nothing
        ever reached it: ``HITLStreamMultiplexer.stream`` catches every
        stream exception and *yields* an :class:`~msg.Error` instead of
        propagating it, and an Agno ``RunErrorEvent`` is serialized to
        :class:`~msg.RunError` on the ordinary event path. Both end the
        run as a failure, both left the loop above falling through to
        ``mark_completed`` — so the "stamp it interrupted so the FE can
        offer Retry/Discard" story only ever fired for user cancels.

        These two message types are the stream's typed failure
        contract; reading them here is what makes a failed run
        recoverable.
        """
        if isinstance(proto, msg.Error):
            return proto.text or "run failed"
        if isinstance(proto, msg.RunError):
            return proto.error or "run failed"
        return None

    # ── The locked body ─────────────────────────────────────────────

    async def _keep_event_log(self, seq_before: int) -> None:
        """Write the event log again once the run has saved its session.

        As for the todo list: an entry appended during the run (the
        visualizer's view) is written into ``session_data`` at once, and
        Agno's end-of-run save of the session it loaded at the start wrote
        over it, so ``get_chat_history`` lost the view after a restart.
        """
        log = self._session.event_log_store
        if log.seq == seq_before:
            return
        result = await self._session.persistence.save_event_log(log.events)
        if not result.ok:
            logger.warning("Could not keep the event log: %s", result.error)

    async def _keep_todos(self, revision_before: int) -> None:
        """Write the todo list again once the run has saved its session.

        ``todo_write`` stores the list in ``session_data`` while the run
        goes, and Agno saves the session it loaded at the run's start when
        the run ends, over it: the list was live, and gone after a restart.
        Written again here, after that save, when the run changed it.
        """
        todos = self._session.todo_store
        if todos.revision == revision_before:
            return
        result = await self._session.persistence.save_todos(todos.items)
        if not result.ok:
            logger.warning("Could not keep the todo list: %s", result.error)

    async def _ensure_mcp(self) -> None:
        """Connect the session's MCP servers before a run uses the team.

        The window's chat path never did this. Only the CLI's
        ``SessionMessageHandler`` called ``ensure_mcp``, so a chat in
        igni's window ran without the configured MCP tools until the
        MCP page happened to call ``ensure_mcp`` itself (campaign
        R209/R669).

        A failure here must not end the chat: the run goes on without
        the MCP tools, and the failure is already recorded in the MCP
        failure cache for the panel to show.
        """
        startup = getattr(self._session, "startup", None)
        ensure = getattr(startup, "ensure_mcp", None)
        if not callable(ensure):
            return
        try:
            await ensure()
        except Exception as exc:
            logger.warning("Could not connect the MCP servers before the run: %s", exc)

    async def _run_locked(
        self, text: str, media: MediaAttachments | None
    ) -> AsyncGenerator[msg.Message, None]:
        """The full pipeline body — invoked by :meth:`run_message`
        inside the outer ``async with self._run_lock`` block.

        Split into named sub-methods so each concern (prompt
        assembly, hook gate, streaming loop, finalize) is one line
        of the body — the old free function was 163 LoC in one
        procedural blob.
        """
        self._transition_to(RunPhase.starting)

        # ``group_policy.required`` and no verified pack at start-up: no
        # agent run, and the person is told how to get one (the run gate).
        refusal = self._gate.reason
        if refusal:
            yield msg.Error(text=refusal)
            self._transition_to(RunPhase.done)
            return

        # Connect the configured MCP servers before the team is read.
        # The window's chat path never did this: only the CLI's
        # ``SessionMessageHandler`` did, so a chat in igni's window
        # had no MCP tools until the MCP page called ``ensure_mcp``
        # (campaign R209/R669). The connect also rebuilds the team, so
        # it has to happen before ``main_team`` is read — otherwise the
        # run would use the team without the MCP tools.
        await self._ensure_mcp()

        team = self._session.main_team

        # Consume the one-shot interrupted-summary + drain any
        # pending-message ids queued during detect_interrupted_run.
        interrupted_summary_text = self._consume_interrupted_note(text)
        await self._pending_journal.drain_queued_drops()
        await self._close_abandoned_pause()

        # Fire UserPromptSubmit hook, before the prompt is built from
        # the text: an extension that hooks it may change the text.
        # Blocks the run when should_continue=False; otherwise
        # optionally appends a <hook-context> block onto the prompt.
        gate = await self._hook_gate.fire_user_prompt_submit(text)
        if not gate.should_continue:
            yield msg.Error(text=gate.block_message or "Message blocked by hook.")
            self._transition_to(RunPhase.done)
            return
        if gate.text is not None:
            text = gate.text

        # Pre-run prompt assembly: mentions, media, URL, learnings,
        # timestamp + interrupted-run note.
        build = await self._prompt_builder.build(
            text, media, interrupted_summary=interrupted_summary_text
        )
        for info in build.info_messages:
            yield info
        message = build.message
        media = build.media

        # A SessionStart hook's context first, then the prompt hook's, as
        # ``igni -m`` sends them: ``<hook-context>`` blocks after the text.
        start_context = self._session.take_session_start_context()
        message += "".join(
            f"\n<hook-context>{context}</hook-context>"
            for context in (start_context, gate.context_message)
            if context
        )

        # Guardrails, on the path the portal actually uses. They inform rather
        # than block, so the caveat is prepended and the model sees it before
        # the user text — the same call ``SessionMessageHandler.handle`` makes.
        # This path did not make it: every portal message reached ``team.arun``
        # unchecked, and agno's PII guardrail hard-raises before the model call
        # rather than telling the model anything, so there was no signal of any
        # kind here.
        guardrail_prefix = await self._session.guardrail_runner.warning_prefix(text)
        if guardrail_prefix:
            message = guardrail_prefix + message

        # Pre-persist the user message so a kill mid-stream doesn't
        # lose it. On success the row is marked completed; on crash
        # it stays pending and the next --continue boot surfaces it.
        pending_id = await self._pending_journal.record(text)
        # Stash on the instance so the sync ``cancel_run`` can
        # stamp the row as interrupted without an async hop. Cleared
        # below on natural completion + in the error branch so a
        # later cancel that arrives after completion is a no-op.
        self._pending_id_for_cancel = pending_id

        # Periodic checkpoint task — see SessionCheckpointer.run_forever.
        checkpoint_task = background.daemon(self.periodic_checkpoint(team), name="run-checkpoint")
        todos_before = self._session.todo_store.revision
        events_before = self._session.event_log_store.seq

        media_kwargs = media.to_kwargs() if media is not None else {}
        self._transition_to(RunPhase.streaming)
        reply: list[str] = []
        paused = False
        try:
            stream_failure: str | None = None
            async for proto in self._stream_factory(
                team.arun(message, stream=True, **media_kwargs)
            ):
                yield proto
                await self._dispatcher.handle(proto, team)
                stream_failure = self._failure_text(proto) or stream_failure
                if isinstance(proto, msg.ContentDelta):
                    reply.append(proto.text)
                paused = self._paused_after(proto, paused)
            # Only once the stream is over: a sub-agent's pause shows up
            # mid-stream while this run goes on, waiting for its answer.
            self._stream_ended_paused = paused
            if stream_failure is not None:
                # The stream reported a failure instead of raising —
                # see :meth:`_failure_text`. Same bookkeeping as the
                # ``except`` branch below, minus the re-raise: the
                # FE has already been handed the error message, so
                # raising here would only cost it its ``StreamEnd``.
                self._pending_id_for_cancel = None
                await self._pending_journal.mark_interrupted(
                    pending_id, "errored", last_error=stream_failure
                )
                self._transition_to(RunPhase.errored)
                return
            if paused:
                # Paused for a person: the run has not finished. The row
                # stays pending, and a cancel still reaches it, until the
                # resumed run ends it (:meth:`continue_run`). Completed
                # here, a crash during the resumed part left nothing for
                # ``get_pending_messages`` or ``get_interrupted_runs``.
                self._paused_pending_id = pending_id
            else:
                # Natural end-of-run — mark the pre-persisted user
                # message as completed.
                await self._pending_journal.mark_completed(pending_id)
                self._pending_id_for_cancel = None
            self._transition_to(RunPhase.finalizing)
        except Exception as exc:
            # Error mid-run: stamp the pending row as an explicit
            # interrupted run so the FE can recover (show a banner
            # with Retry/Discard) on next render. We do this BEFORE
            # re-raising so the mark survives even if the upstream
            # WS handler aborts on the exception.
            self._pending_id_for_cancel = None
            await self._pending_journal.mark_interrupted(pending_id, "errored", last_error=str(exc))
            self._transition_to(RunPhase.errored)
            raise
        finally:
            checkpoint_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await checkpoint_task
            await self._http_client_manager.close_and_replace(team)
            # Queued messages this run took are deleted once the saved
            # conversation holds them (``DurableMessageQueue``).
            if self._message_queue is not None:
                await self._message_queue.settle(self._session.db)
            await self._keep_todos(todos_before)
            await self._keep_event_log(events_before)
            # Memory files the run wrote, whatever tool wrote them.
            mirror = self._session.memory_mirror
            if mirror is not None:
                await asyncio.to_thread(mirror.sync)

        # Fire Stop hook after the natural end-of-run. A run that paused for
        # the person's approval has not ended: Stop fires once the resumed
        # run does (:meth:`continue_run`), with its reply. Fired here, it
        # saw only the text from before the pause, and the resumed run's
        # final answer reached no Stop hook at all.
        if not paused:
            async for proto in self._stop_or_retry(team, "".join(reply)):
                yield proto
        self._transition_to(RunPhase.done)

    @staticmethod
    def _paused_after(proto: msg.Message, paused: bool) -> bool:
        """Whether the run stands paused for a person, after ``proto``."""
        if isinstance(proto, msg.RunPaused):
            return True
        if isinstance(proto, msg.RunCompleted):
            return False
        return paused

    async def _stop_or_retry(self, team: Any, response: str) -> AsyncGenerator[msg.Message, None]:
        """The Stop hook, and the agent sent back while it rejects the reply.

        As ``igni -m`` does (docs/HOOKS.md): the hook sees the reply, and a
        block says why and sends the agent its feedback to revise, at most
        :attr:`StopHookPayload.MAX_RETRIES` times. The backend used to fire
        it with no reply and only show the block.
        """
        for _attempt in range(StopHookPayload.MAX_RETRIES):
            gate = await self._hook_gate.fire_stop(response)
            if gate.should_continue:
                return
            yield msg.Info(text=gate.block_message)
            revised: list[str] = []
            try:
                async for proto in self._stream_factory(
                    team.arun(StopHookPayload.critique(gate.block_message), stream=True)
                ):
                    yield proto
                    await self._dispatcher.handle(proto, team)
                    if isinstance(proto, msg.ContentDelta):
                        revised.append(proto.text)
            except Exception as exc:
                logger.warning("The run a Stop hook sent back failed: %s", exc)
                yield msg.Error(text=f"The revised reply failed: {exc}")
                return
            response = "".join(revised)
