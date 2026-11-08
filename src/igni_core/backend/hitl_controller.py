"""HITL (human-in-the-loop) resolution for one session.

:class:`HitlController` owns the session's approval flow: it answers
the requirements a pause left (:meth:`HitlController.resolve_batch`),
continues the run they paused, drives each team stream through a
:class:`HITLStreamMultiplexer`, and finishes what a previous backend
was continuing when it died. Its collaborators each own one part:

* :class:`AgnoDecisionApplier` — ``req.confirm()`` / ``req.reject()``.
* :class:`HitlPermissions` — permission checks and the sticky choices
  persisted as rules (``controller.permissions``).
* :class:`PauseHandler` — a paused team event into a ``RunPaused``.
* :class:`RunHistory` — closing Agno's stored run after a restart.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from igni_core.backend.agno_decision_applier import AgnoDecisionApplier
from igni_core.backend.cold_spawn_resume import ColdSpawnResume
from igni_core.backend.hitl_permissions import HitlPermissions
from igni_core.backend.hitl_stream_mux import HITLStreamMultiplexer
from igni_core.backend.hitl_tracer import HITLTracer
from igni_core.backend.pause_handler import PauseHandler
from igni_core.backend.pending_requirements_store import PendingRequirementsStore
from igni_core.backend.schemas_hitl import PausedRun, ResolvedBatch, StreamFactory
from igni_core.backend.schemas_pause import PauseHandleResult
from igni_core.backend.schemas_run import StopHookPayload
from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.extension_gate import extension_hook_gate
from igni_core.core.hooks.extension_values import StopValue
from igni_core.core.session.persistence.run_history import RunHistory
from igni_core.protocol import messages as msg

if TYPE_CHECKING:
    from igni_core.core.session import Session

logger = logging.getLogger(__name__)

_HITL_LLM_LOGGER = logging.getLogger("igni_core.llm_calls")

#: Stateless, so one is shared by every controller.
_DECISION_APPLIER = AgnoDecisionApplier()


class HitlController:
    """HITL resolution for a single session (see the module docstring).

    ``stream_factory`` is how a continued run's stream is driven to
    the window. By default it is :meth:`stream_with_subagent`; a test
    passes its own to watch the stream without a multiplexer.
    """

    #: What the session is told about a main-agent run cut off while it
    #: was being continued.
    INTERRUPTED_RUN = (
        "A run was interrupted: igni restarted while it was continuing after "
        "its approval was answered. It was not run again — what it did before "
        "the restart may or may not have taken effect. You can carry on the "
        "conversation."
    )

    def __init__(
        self,
        session: Session,
        store: PendingRequirementsStore,
        *,
        stream_factory: StreamFactory | None = None,
        pause_handler: PauseHandler | None = None,
        tracer: HITLTracer | None = None,
    ) -> None:
        self._session = session
        self._store = store
        self._stream_factory: StreamFactory = stream_factory or self.stream_with_subagent
        self._pause_handler = pause_handler or PauseHandler(
            evaluator=session.permission_evaluator, store=store
        )
        self._tracer = tracer or HITLTracer(enabled=False)

    @property
    def permissions(self) -> HitlPermissions:
        """The session's permission rules, as the HITL flow reads and
        writes them. Built on each use: it holds only the session."""
        return HitlPermissions(self._session)

    # ── Resolution ──────────────────────────────────────────────

    async def resolve_batch(self, decisions: list[msg.HITLDecision]) -> AsyncIterator[msg.Message]:
        """Resolve every requirement from a multi-req pause in one shot.

        Agno's ``acontinue_run`` denies anything not in the
        resolved-requirements list.
        """
        if not decisions:
            return
        batch = self._take_main_requirements(decisions)
        for error in batch.errors:
            yield error

        # Sub-agent approvals a previous backend left: once all of a
        # run's are answered, nothing in this process waits on them —
        # continue the sub-agent and the calls above it here.
        for ready in self._session.sub_agent_hitl.take_ready_restored():
            async for proto in ColdSpawnResume(self._session, self._stream_factory).resume(ready):
                yield proto

        if not batch.requirements:
            return  # everything was sub-agent or failed

        refusal = self._flip_mode(decisions)
        if refusal is not None:
            yield refusal
        async for proto in self._continue(batch):
            yield proto
        async for proto in self._fire_stop_hooks():
            yield proto

    async def resolve_single(
        self, requirement_id: str, action: str, choice: str = msg.HITLChoice.ONCE.value
    ) -> AsyncIterator[msg.Message]:
        """Resolve a single HITL requirement, through :meth:`resolve_batch`
        so the ``acontinue_run(requirements=...)`` call site exists once.

        ``action`` / ``choice`` are ``str`` as on the wire; producers can
        pass :class:`msg.HITLAction` / :class:`msg.HITLChoice` members.
        """
        decision = msg.HITLDecision(requirement_id=requirement_id, action=action, choice=choice)
        async for proto in self.resolve_batch([decision]):
            yield proto

    def _take_main_requirements(self, decisions: list[msg.HITLDecision]) -> ResolvedBatch:
        """Apply each decision the main team owns; route the rest to the
        sub-agent coordinator. Every applied requirement shares one run,
        and the run's auto-resolved requirements join them."""
        batch = ResolvedBatch()
        for d in decisions:
            if self._session.sub_agent_hitl.resolve(d.requirement_id, d.action):
                # Sub-agent coordinator owns this req — drop any
                # main-team entry under the same id.
                self._store.pop(d.requirement_id)
                continue
            entry = self._store.pop(d.requirement_id)
            if entry is None:
                batch.errors.append(msg.Error(text=f"Unknown requirement: {d.requirement_id}"))
                continue
            if not batch.claim(entry.run_id):
                self._store.register(d.requirement_id, entry)  # back as it was, request and all
                batch.errors.append(
                    msg.Error(
                        text=(
                            f"Cross-run HITL batch rejected: "
                            f"requirement {d.requirement_id} belongs to run "
                            f"{entry.run_id} but batch is for run {batch.run_id}"
                        )
                    )
                )
                continue
            applied = _DECISION_APPLIER.apply(d, entry.req)
            if not applied.ok:
                batch.errors.append(msg.Error(text=applied.reason))
                continue
            batch.requirements.append(entry.req)
            persisted = self.permissions.persist_choice(d, entry.req)
            if not persisted.ok and persisted.reason:
                logger.debug(
                    "persist_choice: skipped for %s — %s", d.requirement_id, persisted.reason
                )
        if batch.run_id is not None:
            batch.requirements.extend(self._store.drain_auto_resolved(batch.run_id))
        return batch

    def _flip_mode(self, decisions: list[msg.HITLDecision]) -> msg.Error | None:
        """Apply the batch's mode switch, if a decision carries one.

        Atomic — applied BEFORE ``acontinue_run`` resumes the agent, so
        the very next tool permission check sees the new mode. Closes the
        race where a separate ``/accept on`` fired alongside this batch
        was dispatched concurrently and lost: the agent resumed under the
        OLD mode and re-prompted for the next edit. The first non-empty
        value in the batch wins, so callers need not coordinate.

        A refusal (by the organisation's mode policy, most often) comes
        back as the ``msg.Error`` to show, in the words ``/accept`` would
        use; the approvals themselves still go through.
        """
        target = next(
            (d.set_permission_mode.strip() for d in decisions if d.set_permission_mode.strip()),
            "",
        )
        if not target:
            return None
        switch = self._session.set_permission_mode(target)
        _HITL_LLM_LOGGER.info("resolve_hitl_batch: atomic mode flip → %s (%s)", target, switch.text)
        return msg.Error(text=switch.text) if switch.refused else None

    async def _continue(self, batch: ResolvedBatch) -> AsyncIterator[msg.Message]:
        """Continue the paused run with the answered requirements."""
        run_id = batch.run_id
        _HITL_LLM_LOGGER.info(
            "resolve_hitl_batch: %d req(s) resolved, run_id=%s", len(batch.requirements), run_id
        )
        # Marked while it runs: a backend that dies before the run ends
        # leaves the mark, and the next one reports the run interrupted
        # rather than leave it paused with nothing to answer.
        if run_id:
            self._store.begin_continuing(run_id)
        try:
            async for proto in self._stream_factory(
                self._session.main_team.acontinue_run(
                    run_id=run_id,
                    session_id=self._session.session_id,
                    requirements=batch.requirements,
                    stream=True,
                    stream_events=True,
                )  # type: ignore[call-overload]  # kwargs + agno overload interplay
            ):
                yield proto
        finally:
            if run_id:
                self._store.end_continuing(run_id)

    async def _fire_stop_hooks(self) -> AsyncIterator[msg.Message]:
        """The Stop hook, once the continuation completes: the extensions
        that hook it first, then the configured hooks."""
        verdict = await extension_hook_gate.ask(StopValue(session_id=self._session.session_id))
        if not verdict.allowed:
            yield msg.Info(text=verdict.reason)
            return
        stop_result = await self._session.hook_executor.execute(
            event=HookEvent.STOP.value,
            payload=StopHookPayload(session_id=self._session.session_id).model_dump(),
        )
        if stop_result.message and not stop_result.should_continue:
            yield msg.Info(text=stop_result.message)

    # ── Pause / stream lifecycle ────────────────────────────────

    def handle_pause(self, event: PausedRun) -> PauseHandleResult:
        """Convert a paused team event into a typed
        :class:`PauseHandleResult`, through the controller's one
        :class:`PauseHandler` (the multiplexer shares it)."""
        return self._pause_handler.handle(event)

    async def stream_with_subagent(
        self, team_stream: AsyncIterator[Any]
    ) -> AsyncIterator[msg.Message]:
        """Drive one team-stream lifecycle through a fresh
        :class:`HITLStreamMultiplexer`.

        The mux is instantiated per stream because its ``asyncio.Queue``
        + drain-task state is per-call — sharing would leak state across
        concurrent streams.
        """
        mux = HITLStreamMultiplexer(
            session=self._session,
            store=self._store,
            pause_handler=self._pause_handler,
            tracer=self._tracer,
        )
        async for proto in mux.stream(team_stream):
            yield proto

    def sweep_run(self, run_id: str) -> None:
        """Drop pending + auto-resolved entries for a finished run."""
        self._store.sweep_run(run_id)

    # ── What survives a reload or a restart ─────────────────────

    def waiting_messages(self) -> list[msg.RunPaused]:
        """Every approval of this session still waiting on the person,
        as the ``RunPaused`` a window shows a dialog for.

        Sent to a window when it attaches, so a dialog survives both a
        reload and a backend restart (the approvals are read back from
        the journal at start): the main agent's, then its sub-agents'.
        """
        return [
            *self._store.waiting_messages(),
            *self._session.sub_agent_hitl.waiting_requests(),
        ]

    async def resume_interrupted(self) -> AsyncIterator[msg.Message]:
        """Finish what a previous backend was continuing when it died.

        A main-agent run whose approvals had been answered left its
        ``continuing`` mark: the session is told it was interrupted, and
        Agno's stored run is closed rather than left paused. A sub-agent
        or team left its spawn link: it is reported failed to the call
        waiting on it, and that call's run goes on
        (``ColdSpawnResume.interrupted``). None is run again; each is
        taken once.
        """
        for run_id in self._store.take_interrupted_runs():
            if await self._close_interrupted_run(run_id):
                yield msg.Info(text=self.INTERRUPTED_RUN)
        for link in self._session.sub_agent_hitl.take_interrupted():
            async for proto in ColdSpawnResume(self._session, self._stream_factory).interrupted(
                link
            ):
                yield proto

    async def _close_interrupted_run(self, run_id: str) -> bool:
        """Close Agno's stored run ``run_id``; ``True`` when it was found."""
        db = self._session.main_team.db
        if db is None:
            return False
        return await RunHistory(db).close_interrupted(
            self._session.session_id, run_id, self.INTERRUPTED_RUN
        )


__all__ = ["HitlController"]
