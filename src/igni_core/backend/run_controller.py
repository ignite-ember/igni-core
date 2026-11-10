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
from typing import TYPE_CHECKING, TypeVar

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
from igni_core.core.activity import background
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.session.pending_messages import InterruptedReason
from igni_core.core.session.queued_messages import DurableMessageQueue
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.utils.media_schemas import MediaAttachments
from igni_core.protocol import messages as msg

if TYPE_CHECKING:
    from igni_core.core.session import Session
    from igni_core.core.session.pending_messages import PendingMessageStore


logger = logging.getLogger(__name__)


_T = TypeVar("_T")


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

    def __init__(
        self,
        session: Session,
        pending_store: PendingMessageStore,
        message_queue: DurableMessageQueue | None,
        stream_factory: StreamFactory,
        supervisor: ProcessSupervisor,
        gate: RunGate,
        http_client_manager: ModelHttpClientManager | None = None,
    ) -> None:
        self._session = session
        self._message_queue = message_queue
        self._stream_factory = stream_factory
        self._supervisor = supervisor
        self._gate = gate
        self._pending_journal = PendingMessageJournal(pending_store, session.session_id)
        self._http_client_manager = http_client_manager or ModelHttpClientManager()
        self._prompt_builder = PromptBuilder(session)
        self._hook_gate = RunHookGate(session.hook_executor, session.session_id)
        self._dispatcher = StreamEventDispatcher(session, self.checkpoint)
        self._phase = RunPhase.idle
        self._interrupted_summary: InterruptedRunSummary | None = None
        # Serialisation + cancellation state — single owner (Pattern 1).
        self._run_lock = asyncio.Lock()
        self._current_run_task: asyncio.Task | None = None
        # The task ``cancel_run`` cancelled, so the work it was running can
        # tell the person's cancel from any other.
        self._cancelled_by_person: asyncio.Task | None = None
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
        """The ``asyncio.Task`` currently iterating the run body, or
        ``None`` when idle. Set/cleared inside :meth:`run_message`
        so tests inspecting the field see the same lifecycle as
        production."""
        return self._current_run_task

    def is_processing(self) -> bool:
        """True when a run is holding state a second submit would
        race on. Wire-compatible with the previous
        ``BackendServer._processing`` bool."""
        return self._phase.is_active()

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
            self._current_run_task = asyncio.current_task()
            try:
                async for proto in self._run_locked(text, media):
                    yield proto
            except asyncio.CancelledError:
                yield msg.Info(text="Run cancelled by user.")
            finally:
                self._current_run_task = None
                # A cancelled run reaches none of ``_run_locked``'s
                # terminal transitions: ``CancelledError`` is a
                # BaseException, so its ``except Exception`` branch
                # doesn't fire and its ``finally`` only tears down the
                # checkpoint task + httpx client. The phase stayed
                # ``streaming`` forever, which meant ``is_processing()``
                # stayed True — the FE's ``get_processing`` reported the
                # session busy for the rest of the process's life and
                # ``SessionPool.evict_idle`` could never reclaim the
                # runtime. Mapped here rather than in the ``except``
                # above so a generator closed without ever resuming
                # (the consumer breaking out of its ``async for``, or a
                # re-raised cancel on the ``yield``) lands the same way.
                if self._phase.is_active():
                    self._transition_to(RunPhase.done)

    async def continue_run(
        self, stream: AsyncIterator[msg.Message]
    ) -> AsyncGenerator[msg.Message, None]:
        """A paused run, resumed once its approvals are answered, as this
        session's run: ``cancel_run`` reaches it and ``get_processing``
        reports it, as for the run that paused.

        Not under the run lock: a sub-agent's approval is answered while
        its parent run still holds it. Then the parent is the session's
        run already, and the stream passes through untouched.
        """
        if self._current_run_task is not None:
            async for proto in stream:
                yield proto
            return
        self._current_run_task = asyncio.current_task()
        self._transition_to(RunPhase.streaming)
        try:
            async for proto in stream:
                yield proto
        except asyncio.CancelledError:
            yield msg.Info(text="Run cancelled by user.")
        finally:
            self._current_run_task = None
            if self._phase.is_active():
                self._transition_to(RunPhase.done)

    async def run_unstreamed(self, work: Callable[[], Awaitable[_T]]) -> _T:
        """Work on the session's team outside a streamed turn — a scheduled
        task run now — as the session's run.

        One at a time with the turns (the run lock), reported by
        ``get_processing``, stopped by ``cancel_run``: a cancel ends it
        with :class:`RunCancelledError` for whoever asked for the work.
        """
        async with self._run_lock:
            task = asyncio.current_task()
            self._current_run_task = task
            self._transition_to(RunPhase.streaming)
            try:
                return await work()
            except asyncio.CancelledError:
                if task is None or self._cancelled_by_person is not task:
                    raise
                task.uncancel()
                raise RunCancelledError("Run cancelled by user.") from None
            finally:
                self._current_run_task = None
                self._cancelled_by_person = None
                if self._phase.is_active():
                    self._transition_to(RunPhase.done)

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
        """
        self._cancel_own_foreground()

        self._stamp_interrupted("cancelled", last_error=None)

        task = self._current_run_task
        if task and not task.done():
            logger.info("Cancelling run task %s", task.get_name())
            self._cancelled_by_person = task
            task.cancel()
            return True
        return False

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
        if self._phase is not RunPhase.streaming:
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

        team = self._session.main_team

        # Consume the one-shot interrupted-summary + drain any
        # pending-message ids queued during detect_interrupted_run.
        interrupted_summary_text = self._consume_interrupted_note(text)
        await self._pending_journal.drain_queued_drops()

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

        if gate.context_message:
            message = f"{message}\n<hook-context>{gate.context_message}</hook-context>"

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

        media_kwargs = media.to_kwargs() if media is not None else {}
        self._transition_to(RunPhase.streaming)
        try:
            stream_failure: str | None = None
            async for proto in self._stream_factory(
                team.arun(message, stream=True, **media_kwargs)
            ):
                yield proto
                await self._dispatcher.handle(proto, team)
                stream_failure = self._failure_text(proto) or stream_failure
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
            # Memory files the run wrote, whatever tool wrote them.
            mirror = self._session.memory_mirror
            if mirror is not None:
                await asyncio.to_thread(mirror.sync)

        # Fire Stop hook after the natural end-of-run.
        stop_gate = await self._hook_gate.fire_stop()
        if not stop_gate.should_continue and stop_gate.block_message:
            yield msg.Info(text=stop_gate.block_message)
        self._transition_to(RunPhase.done)
