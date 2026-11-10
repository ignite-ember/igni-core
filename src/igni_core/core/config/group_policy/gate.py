"""Whether this process may run agent sessions.

One gate per backend process, built by the composition root and handed
to the parts that start agent runs — the run controller, the scheduler
and the workflow agent bridge. It is closed at start-up when the
organisation's policy is required and no verified pack could be had,
and it is the gate's own object that says so: nothing reads a class
attribute to find out whether a run may start.
"""

from __future__ import annotations

from enum import StrEnum


class GatePhase(StrEnum):
    """Whether this process may run agent sessions."""

    open = "open"
    closed = "closed"


class RunGate:
    """Whether this process may run agent sessions.

    Closed by the backend at start-up when ``group_policy.required`` is
    set and no verified pack could be applied. The backend still boots —
    the person has to be able to sign in, which is how a valid pack is
    fetched — but every place that starts an agent run asks here first
    and refuses with :attr:`reason`.

    Stays closed for the life of the process: the settings this process
    loaded were built without the policy, so a pack that arrives later
    cannot make *them* compliant. Restarting the backend with
    compliant settings is the way out.
    """

    def __init__(self) -> None:
        self._phase = GatePhase.open
        self._reason: str | None = None

    @property
    def phase(self) -> GatePhase:
        """Whether an agent run may start."""
        return self._phase

    @property
    def reason(self) -> str | None:
        """Why an agent run is refused; ``None`` while the gate is open."""
        return self._reason

    def close(self, reason: str) -> None:
        """Refuse every agent run from now on, and say why."""
        self._phase = GatePhase.closed
        self._reason = reason


__all__ = ["GatePhase", "RunGate"]
