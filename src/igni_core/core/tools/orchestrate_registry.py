"""The registry of the sub-agent runs that are still going.

Agno's cooperative cancel (``Agent.cancel_run(run_id)``) flags
exactly one run_id — the top-level team's cancel does not propagate
to sub-agents Agno assigned distinct run_ids (the visualizer, the
editor, every specialist). ``BackendServer.cancel_run`` iterates this
registry and cancels every entry, so a stuck sub-agent (a visualizer
retrying a truncated tool call in a loop) actually stops when the
person hits ESC.

Adds happen inside the stream handler on the first event carrying a
``run_id``; removes happen in its ``finally``, so a mid-stream
exception still cleans up.

The class replaces the ``OrchestrateTools._active_subagent_runs``
classvar it grew out of: an injectable, instance-scoped collaborator
is easier to reason about under concurrent test runs, and it says
what it is for.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator


class SubAgentRegistry:
    """The run ids of the sub-agents that are in flight."""

    __slots__ = ("_active",)

    def __init__(self, initial: Iterable[str] | None = None) -> None:
        self._active: set[str] = set(initial or ())

    def register(self, run_id: str) -> None:
        """Note that a run is going, so a cancel can reach it."""
        if run_id:
            self._active.add(run_id)

    def discard(self, run_id: str) -> None:
        """Note that a run has finished."""
        self._active.discard(run_id)

    def __contains__(self, run_id: object) -> bool:
        return run_id in self._active

    def __iter__(self) -> Iterator[str]:
        return iter(tuple(self._active))

    def __len__(self) -> int:
        return len(self._active)

    def snapshot(self) -> set[str]:
        """Copy of the live set — safe to iterate while cancels fire."""
        return set(self._active)

    def clear(self) -> None:
        self._active.clear()


__all__ = ["SubAgentRegistry"]
