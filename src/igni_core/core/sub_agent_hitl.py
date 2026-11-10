"""Bridge HITL approval between sub-agents and the user-facing TUI.

When the orchestrator's `spawn_agent` runs a specialist, the specialist's
tool calls run inside the parent agent's tool execution. Their
``RunPausedEvent``s never reach the backend's main run loop — Agno only
streams them out of the *current* `arun()` call, and the parent's tool
function eats them.

This coordinator gives them a path out: the sub-agent's stream handler
pushes the pending requirements here, the backend's main run loop polls
for them and forwards them to the FE as ordinary ``HITLRequest`` events,
and :meth:`HitlController.resolve_single` routes confirmation back into
the coordinator. The sub-agent's stream handler awaits the resolution,
then resumes the sub-agent via ``acontinue_run``.

Scope: per-Session. One instance lives on ``Session`` and is injected
into ``OrchestrateTools``. Threading: every shared field is touched only
from the asyncio event loop; no locks needed.

Durable when given an :class:`ApprovalJournal` (:meth:`attach_journal`;
the backend always does): each request is a row from the moment it is
pushed until it is answered. After a restart :meth:`restore` brings the
requests back with fresh events, so they are shown again and can be
answered. The ``spawn_agent`` call that was waiting on one died with the
backend, so nothing resumes on the answer — its parent run is surfaced
as interrupted, like any run a restart cut off — but the person is not
left with a question nobody can take.

The coordinator itself only orchestrates; each part of the flow lives
in its own module under :mod:`igni_core.core.hitl`:

* :attr:`pending` — the questions waiting for a person.
* :attr:`ledger` — the approvals' durable side (the journal).
* :attr:`turns` — the finished calls' results a continued turn needs.
* :attr:`relay` — who is relaying questions to a person right now.

The coordinator stays the one thing the rest of the code talks to:
every question, relay and turn is reached through its methods, and
each of them hands straight over to the part that owns the state.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable

from agno.run.requirement import RunRequirement

from igni_core.core.approvals.declined import declined_note
from igni_core.core.approvals.journal import ApprovalJournal
from igni_core.core.approvals.records import SpawnLink
from igni_core.core.hitl.ledger import ApprovalLedger
from igni_core.core.hitl.models import (
    DetailUpdate,
    HitlRequirement,
    PendingEntry,
    PendingHandle,
    RestoredRun,
)
from igni_core.core.hitl.registry import PendingRegistry
from igni_core.core.hitl.relay import RelayCounter
from igni_core.core.hitl.trace import HitlTrace
from igni_core.core.hitl.turns import TurnLedger
from igni_core.core.tools.spawn_context import SpawnDeadline
from igni_core.protocol import messages as msg

#: Where the coordinator's own pushes are recorded, whatever the layer
#: keeps in its logs.
_LLM_LOGGER = logging.getLogger("igni_core.llm_calls")


class SubAgentHITLCoordinator:
    """Per-session registry of sub-agent HITL requirements.

    See the module docstring for the full flow.
    """

    def __init__(self) -> None:
        self.pending = PendingRegistry()
        self.turns = TurnLedger()
        self.ledger = ApprovalLedger(self.turns)
        self.relay = RelayCounter()
        self._trace = HitlTrace()

    @property
    def new_arrival(self) -> asyncio.Event:
        """Set when something the frontend has not seen arrives — a new
        question, or a better explanation for one it has."""
        return self.pending.new_arrival

    # ── Durability ────────────────────────────────────────────────

    def attach_journal(self, journal: ApprovalJournal, session_id: Callable[[], str]) -> None:
        """Write requests through to ``journal`` from now on."""
        self.ledger.attach(journal, session_id)

    def restore(self, session_id: str) -> int:
        """Bring back what ``session_id`` left waiting; returns how many."""
        restored = self.ledger.restore(session_id)
        for stored in restored:
            if stored.request is not None and stored.request.details:
                stored.requirement.details = stored.request.details  # type: ignore[attr-defined]
            # Already "surfaced": it reaches a window by
            # :meth:`waiting_requests`, when one attaches.
            self.pending.add(
                stored.requirement_id,
                PendingEntry(
                    requirement=stored.requirement,
                    run_id=stored.run_id,
                    agent_path=stored.agent_path,
                    surfaced=True,
                ),
            )
            self.pending.mark_restored(stored.requirement_id)
        return len(restored)

    def take_ready_restored(self) -> list[RestoredRun]:
        """Restored runs every one of whose approvals is now answered.

        Each is returned once, with its requirements as decided and the
        link the journal kept, for :class:`ColdSpawnResume` to continue.
        """
        ready = self.pending.take_ready()
        for run in ready:
            link = self.ledger.spawn_link(run.run_id)
            if link is not None:
                run.link = link
        return ready

    def take_interrupted(self) -> list[SpawnLink]:
        """Links whose answered run was still being continued when the
        previous backend died — each returned once."""
        return self.ledger.take_interrupted()

    def forget_spawn(self, run_id: str) -> None:
        """The run is over: its spawn link is of no use to anybody."""
        self.ledger.forget_spawn(run_id)

    # ── Questions ─────────────────────────────────────────────────

    def waiting_requests(self) -> list[msg.RunPaused]:
        """One ``RunPaused`` per sub-agent run with requests not yet
        answered — what a window attaching to the session is shown."""
        return self.pending.waiting_requests()

    @property
    def waiting_count(self) -> int:
        """Sub-agent runs blocked on a person right now — each one's
        spawn is doing nothing but wait."""
        return self.pending.waiting_count

    def has_unresolved(self) -> bool:
        """Whether any question is still unanswered."""
        return self.pending.has_unresolved

    def request_for(self, req_id: str) -> msg.HITLRequest | None:
        """The request a window is shown for ``req_id``."""
        return self.pending.request_for(req_id)

    def list_new_pending(self) -> list[PendingHandle]:
        """The questions not yet surfaced to the frontend; mark them shown.

        Called by the backend run loop. Idempotent across calls — once a
        question is surfaced it will not be returned again.
        """
        return self.pending.take_unsurfaced()

    def take_detail_updates(self) -> list[DetailUpdate]:
        """The explanations for still-open questions; each returned once."""
        return self.pending.take_updates()

    def hand_back(self, entries: list[PendingHandle]) -> None:
        """Return taken-but-unshown questions for the next relay to surface."""
        self.pending.hand_back(entries)

    async def push_requirement(
        self,
        req: RunRequirement | HitlRequirement,
        run_id: str,
        agent_path: list[str] | None = None,
        session_id: str = "",
    ) -> str:
        """Called from the sub-agent stream handler when its run pauses.

        ``session_id`` is the sub-agent run's own Agno session: with the
        calls waiting above it (:class:`SpawnCalls`) it is what a backend
        that starts cold needs to continue the run.
        """
        path = list(agent_path or [])
        _LLM_LOGGER.info(
            "coord.push_requirement: id_self=%s run_id=%s path=%s",
            id(self),
            run_id,
            path,
        )
        self._trace.record(
            f"coord.push_requirement: coord_id={id(self)} run_id={run_id} path={path}"
        )
        requirement_id = str(uuid.uuid4())[:8]
        entry = PendingEntry(requirement=req, run_id=run_id, agent_path=path)
        self.pending.add(requirement_id, entry)
        self.ledger.record(requirement_id, entry)
        self.ledger.record_spawn(run_id, session_id, path)
        self.pending.new_arrival.set()
        return requirement_id

    def update_details(self, req_id: str, details: str) -> None:
        """Set why a pending question is being asked, on screen too.

        Not yet surfaced: it goes out with the new text. Already
        surfaced: queued for the relay to send as an update. Answered or
        gone: nothing to do.
        """
        entry = self.pending.entry(req_id)
        if entry is None or entry.event.is_set():
            return
        entry.explain(details)
        self.ledger.update_request(req_id, entry.request(req_id))
        if entry.surfaced:
            self.pending.queue_update(DetailUpdate(requirement_id=req_id, details=details))
            self.pending.new_arrival.set()

    def resolve(self, req_id: str, action: msg.HITLAction | str) -> bool:
        """Called from :meth:`HitlController.resolve_batch`. Returns
        True if handled here.

        ``action`` is the person's decision — ``confirm`` or ``reject``.
        It travels on the wire as a plain string, so it is read as
        :class:`msg.HITLAction` here: a member stays itself, and any
        other string becomes the action it names (``unknown`` for a
        string that names none).
        """
        entry = self.pending.entry(req_id)
        if entry is None:
            return False
        if msg.HITLAction(action).is_confirm():
            entry.requirement.confirm()
        else:
            tool_execution = getattr(entry.requirement, "tool_execution", None)
            entry.requirement.reject(
                note=declined_note(str(getattr(tool_execution, "tool_name", "") or ""))
            )
        entry.event.set()
        # Answered: a restart from here on must not ask again. A
        # restored request stays until its run is continued
        # (:meth:`take_ready_restored`): nothing in this process waits
        # on it.
        self.ledger.forget(req_id)
        return True

    async def wait_resolved(self, req_id: str) -> RunRequirement | HitlRequirement:
        """Called from the sub-agent stream handler. Blocks until user resolves."""
        entry = self.pending.entry(req_id)
        if entry is None:
            raise KeyError(req_id)
        self.pending.mark_awaited(req_id, True)
        try:
            # However long the person takes: approvals do not expire.
            with SpawnDeadline.waiting_on_person():
                await entry.event.wait()
        finally:
            self.pending.mark_awaited(req_id, False)
        return entry.requirement

    def cleanup(self, req_id: str) -> None:
        """Drop the entry once spawn_agent has resumed past it."""
        self.pending.remove(req_id)
        # The spawn link stays until the run's stream ends: a restart
        # while the answered run is still being continued must find it
        # (:meth:`take_interrupted`).
        self.ledger.forget(req_id)

    # ── Relays ────────────────────────────────────────────────────

    def open_relay(self) -> None:
        """A forwarder starts relaying questions to a person."""
        self.relay.open()

    def close_relay(self) -> None:
        """A forwarder opened with :meth:`open_relay` has stopped."""
        self.relay.close()

    @property
    def has_relay(self) -> bool:
        """Whether a requirement pushed now would reach a person."""
        return self.relay.has_relay

    # ── Turns ──────────────────────────────────────────────────────

    def tool_finished(self, run_id: str, tool_call_id: str, result: str) -> None:
        """A call of ``run_id`` finished (``core/hooks/turn_results.py``).

        Kept for the turn a spawn of that run may be waiting in; written
        to the journal once a spawn of the run waits on a person, so a
        restart can continue the turn with every call's result.
        """
        self.turns.tool_finished(run_id, tool_call_id, result)

    def turn_results(self, run_id: str) -> dict[str, str]:
        """The stored results of ``run_id``'s finished calls."""
        return self.turns.results(run_id)

    def forget_turn(self, run_id: str) -> None:
        """``run_id`` is over: its results are of no use to anybody."""
        self.turns.forget(run_id)


__all__ = ["SubAgentHITLCoordinator"]
