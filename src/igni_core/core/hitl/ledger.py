"""The approvals' durable side: the journal, and what it hands back.

Every approval a sub-agent raises is written to the project's approval
journal the moment it is pushed, and deleted when it is answered. A
backend that starts cold reads them back
(:meth:`ApprovalLedger.restore`) so the question is asked again, and
keeps the spawn links of the runs that were waiting, so the answer can
be carried on.
"""

from __future__ import annotations

from collections.abc import Callable

from agno.run.requirement import RunRequirement

from igni_core.core.approvals.journal import ApprovalJournal
from igni_core.core.approvals.records import ApprovalOrigin, SpawnLink, StoredApproval
from igni_core.core.hitl.models import PendingEntry
from igni_core.core.hitl.turns import TurnLedger
from igni_core.core.tools.spawn_context import SpawnCalls
from igni_core.protocol import messages as msg


class ApprovalLedger:
    """What the approval journal keeps for the sub-agent approvals."""

    def __init__(self, turns: TurnLedger) -> None:
        self._turns = turns
        self._journal: ApprovalJournal | None = None
        self._session_id: Callable[[], str] = lambda: ""
        #: Spawn links a previous backend left with no approval waiting:
        #: their run was being continued when it died.
        self._interrupted: list[SpawnLink] = []

    def attach(self, journal: ApprovalJournal, session_id: Callable[[], str]) -> None:
        """Write approvals through to ``journal`` from now on."""
        self._journal = journal
        self._session_id = session_id
        self._turns.attach_journal(journal)

    def restore(self, session_id: str) -> list[StoredApproval]:
        """What ``session_id`` left waiting: its sub-agent approvals.

        The spawn links of runs that are not waiting any more are kept
        aside — their run was being continued when the previous backend
        died, and the caller has to be told.
        """
        if self._journal is None:
            return []
        waiting = self._journal.waiting(session_id)
        asked = {s.run_id for s in waiting if s.origin is ApprovalOrigin.SUB_AGENT}
        self._interrupted = [
            link for link in self._journal.spawns.of_session(session_id) if link.run_id not in asked
        ]
        return [stored for stored in waiting if stored.origin is ApprovalOrigin.SUB_AGENT]

    def record(self, requirement_id: str, entry: PendingEntry) -> None:
        """Write the approval ``entry`` holds to the journal."""
        if self._journal is None:
            return
        # A single-call safety question is not Agno's and cannot be
        # serialised; it lives in memory only.
        if not isinstance(entry.requirement, RunRequirement):
            return
        self._journal.record(
            requirement_id=requirement_id,
            session_id=self._session_id(),
            run_id=entry.run_id,
            origin=ApprovalOrigin.SUB_AGENT,
            requirement=entry.requirement,
            request=entry.request(requirement_id),
        )

    def update_request(self, requirement_id: str, request: msg.HITLRequest) -> None:
        """What the window is shown changed (a better explanation)."""
        if self._journal is not None:
            self._journal.update_request(requirement_id, request)

    def forget(self, requirement_id: str) -> None:
        """Answered: the approval is no longer waiting."""
        if self._journal is not None:
            self._journal.forget(requirement_id)

    def forget_spawn(self, run_id: str) -> None:
        """The run is over: its spawn link is of no use to anybody."""
        if self._journal is not None:
            self._journal.spawns.forget(run_id)

    def record_spawn(self, run_id: str, agent_session_id: str, agent_path: list[str]) -> None:
        """Remember the paused sub-agent run ``run_id``.

        ``agent_session_id`` is the sub-agent run's own Agno session:
        with the calls waiting above it (:class:`SpawnCalls`) it is what
        a backend that starts cold needs to continue the run.
        """
        if self._journal is None:
            return
        parents = SpawnCalls.current()
        if not parents or not agent_session_id or not agent_path:
            return
        for parent in parents:
            self._turns.link(parent.run_id)
        self._journal.spawns.record(
            SpawnLink(
                run_id=run_id,
                session_id=self._session_id(),
                agent_session_id=agent_session_id,
                agent_name=agent_path[-1],
                parents=parents,
            )
        )

    def spawn_link(self, run_id: str) -> SpawnLink | None:
        """The link the journal kept for ``run_id``."""
        return self._journal.spawns.get(run_id) if self._journal is not None else None

    def take_interrupted(self) -> list[SpawnLink]:
        """Links whose answered run was still being continued when the
        previous backend died — each returned once."""
        interrupted, self._interrupted = self._interrupted, []
        return interrupted


__all__ = ["ApprovalLedger"]
