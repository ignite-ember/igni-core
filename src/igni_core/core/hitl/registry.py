"""The questions waiting for a person.

The registry owns what is in flight: each question's entry (the
requirement it holds, the run it belongs to, the agents that asked),
which questions have been shown already, which were read back after a
restart, and the better explanations that arrived for questions
already on screen.

``new_arrival`` is the signal the backend waits on: it is set when
something arrived that the frontend has not seen yet — a new question,
or a better explanation for one it has — and cleared once the frontend
has taken it.
"""

from __future__ import annotations

import asyncio

from agno.run.requirement import RunRequirement

from igni_core.core.hitl.models import (
    DetailUpdate,
    PendingEntry,
    PendingHandle,
    RestoredRun,
)
from igni_core.protocol import messages as msg


class PendingRegistry:
    """The sub-agent questions waiting for a person."""

    def __init__(self) -> None:
        self._entries: dict[str, PendingEntry] = {}
        #: Questions read back after a restart: nothing waits on them.
        self._restored: set[str] = set()
        #: Questions a stream handler is blocked on right now.
        self._awaited: set[str] = set()
        #: Better explanations for questions already on screen.
        self._updates: list[DetailUpdate] = []
        #: Set whenever something the frontend has not seen arrives.
        self.new_arrival = asyncio.Event()

    def add(self, requirement_id: str, entry: PendingEntry) -> None:
        """A question is waiting."""
        self._entries[requirement_id] = entry

    def entry(self, requirement_id: str) -> PendingEntry | None:
        """The question ``requirement_id`` names, if it is still waiting."""
        return self._entries.get(requirement_id)

    def ids(self) -> list[str]:
        """The ids of the questions still waiting, in arrival order."""
        return list(self._entries)

    def remove(self, requirement_id: str) -> PendingEntry | None:
        """The question is over: drop it."""
        return self._entries.pop(requirement_id, None)

    def mark_restored(self, requirement_id: str) -> None:
        """This question was read back after a restart."""
        self._restored.add(requirement_id)

    def mark_awaited(self, requirement_id: str, waiting: bool) -> None:
        """Whether a stream handler is blocked on this question."""
        if waiting:
            self._awaited.add(requirement_id)
        else:
            self._awaited.discard(requirement_id)

    @property
    def waiting_count(self) -> int:
        """Sub-agent runs blocked on a person right now — each one's
        spawn is doing nothing but wait."""
        return sum(
            1
            for requirement_id in self._awaited
            if (entry := self._entries.get(requirement_id)) is not None and not entry.event.is_set()
        )

    @property
    def has_unresolved(self) -> bool:
        """Whether any question is still unanswered."""
        return any(not entry.event.is_set() for entry in self._entries.values())

    def take_unsurfaced(self) -> list[PendingHandle]:
        """The questions not yet shown to the frontend; mark them shown.

        Idempotent across calls — once a question is surfaced it will
        not be returned again.
        """
        handles: list[PendingHandle] = []
        for requirement_id, entry in self._entries.items():
            if not entry.surfaced and not entry.event.is_set():
                entry.surfaced = True
                handles.append(PendingHandle(requirement_id=requirement_id, entry=entry))
        # The arrival event is one-shot — the caller resets after consuming.
        self.new_arrival.clear()
        return handles

    def hand_back(self, handles: list[PendingHandle]) -> None:
        """Return taken-but-unshown questions for the next relay to surface."""
        for handle in handles:
            handle.entry.surfaced = False
        self.new_arrival.set()

    def queue_update(self, update: DetailUpdate) -> None:
        """A better explanation for a question already on screen."""
        self._updates.append(update)

    def take_updates(self) -> list[DetailUpdate]:
        """The explanations for still-open questions; each returned once."""
        updates = [
            update
            for update in self._updates
            if (entry := self._entries.get(update.requirement_id)) is not None
            and not entry.event.is_set()
        ]
        self._updates.clear()
        return updates

    def request_for(self, requirement_id: str) -> msg.HITLRequest | None:
        """The request a window is shown for ``requirement_id``."""
        entry = self._entries.get(requirement_id)
        return None if entry is None else entry.request(requirement_id)

    def waiting_requests(self) -> list[msg.RunPaused]:
        """One ``RunPaused`` per sub-agent run with questions not yet
        answered — what a window attaching to the session is shown."""
        by_run: dict[str, list[msg.HITLRequest]] = {}
        for requirement_id, entry in self._entries.items():
            if entry.event.is_set():
                continue
            request = entry.request(requirement_id)
            by_run.setdefault(entry.run_id, []).append(request)
        return [msg.RunPaused(run_id=run_id, requirements=reqs) for run_id, reqs in by_run.items()]

    def take_ready(self) -> list[RestoredRun]:
        """Restored runs every one of whose approvals is now answered.

        Each is returned once, with its requirements as decided, for
        the caller to continue the run.
        """
        by_run: dict[str, list[str]] = {}
        for requirement_id in self._restored:
            entry = self._entries.get(requirement_id)
            if entry is not None:
                by_run.setdefault(entry.run_id, []).append(requirement_id)
        ready: list[RestoredRun] = []
        for run_id, requirement_ids in by_run.items():
            entries = [self._entries[requirement_id] for requirement_id in requirement_ids]
            if not all(entry.event.is_set() for entry in entries):
                continue
            requirements = [
                entry.requirement
                for entry in entries
                if isinstance(entry.requirement, RunRequirement)
            ]
            for requirement_id in requirement_ids:
                self._entries.pop(requirement_id, None)
            self._restored.difference_update(requirement_ids)
            ready.append(RestoredRun(run_id=run_id, requirements=requirements))
        return ready


__all__ = ["PendingRegistry"]
