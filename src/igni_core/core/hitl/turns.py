"""The finished calls' results a continued turn needs.

An assistant turn can make several tool calls at once: a
``spawn_agent`` whose sub-agent waits on a person, beside calls that
finish — or are still running — while it waits. If the backend dies in
the meantime, continuing that turn later needs every call's result:
the finished ones as they were, the unfinished ones marked as cut off.

The results are kept in memory while the run lives, and written to the
approval journal once a spawn of that run waits on a person, so a
backend that starts cold can read them back.
"""

from __future__ import annotations

from igni_core.core.approvals.journal import ApprovalJournal


class TurnLedger:
    """The finished calls of the turns a spawn waits under."""

    #: Runs whose finished calls are remembered in memory at once.
    TURNS_KEPT = 64

    def __init__(self) -> None:
        self._results: dict[str, dict[str, str]] = {}
        #: Runs with a spawn waiting under them: their results are written.
        self._linked: set[str] = set()
        self._journal: ApprovalJournal | None = None

    def attach_journal(self, journal: ApprovalJournal) -> None:
        """Write the results through to ``journal`` from now on."""
        self._journal = journal

    def tool_finished(self, run_id: str, tool_call_id: str, result: str) -> None:
        """A call of ``run_id`` finished."""
        turn = self._results.setdefault(run_id, {})
        turn[tool_call_id] = result
        while len(self._results) > self.TURNS_KEPT:
            self._results.pop(next(iter(self._results)))
        if self._journal is not None and run_id in self._linked:
            self._journal.turns.record(run_id, tool_call_id, result)

    def results(self, run_id: str) -> dict[str, str]:
        """The stored results of ``run_id``'s finished calls."""
        return self._journal.turns.of_run(run_id) if self._journal is not None else {}

    def forget(self, run_id: str) -> None:
        """``run_id`` is over: its results are of no use to anybody."""
        self._linked.discard(run_id)
        self._results.pop(run_id, None)
        if self._journal is not None:
            self._journal.turns.forget(run_id)

    def link(self, run_id: str) -> None:
        """``run_id`` has a spawn waiting under it: keep its results.

        What is in memory is written to the journal now, so a backend
        that starts cold can read it back.
        """
        if run_id in self._linked:
            return
        self._linked.add(run_id)
        if self._journal is None:
            return
        for call_id, result in self._results.get(run_id, {}).items():
            self._journal.turns.record(run_id, call_id, result)


__all__ = ["TurnLedger"]
