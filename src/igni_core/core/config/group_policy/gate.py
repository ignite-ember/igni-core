"""Whether this process may run agent sessions.

One gate per backend process, built by the composition root and handed
to the parts that start agent runs — the run controller, the scheduler
and the workflow agent bridge. It is closed at start-up when the
organisation's policy is required and no verified pack could be had, or
when the person's group runs igni for them in the cloud, and it is the
gate's own object that says so: nothing reads a class attribute to find
out whether a run may start.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from igni_core.core.config.group_policy.pack import GroupMode
from igni_core.core.layer import layers


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

    Closed too when the verified pack's ``mode`` is ``cloud`` and this
    igni runs on the person's computer (:meth:`close_for_mode`): their
    organisation runs igni for them in the cloud, and the desktop app
    connects there instead. Sign-in and status still work.

    Stays closed for the life of the process: the settings this process
    loaded were built without the policy, so a pack that arrives later
    cannot make *them* compliant. Restarting the backend with
    compliant settings is the way out.
    """

    #: Why a run is refused on a computer whose group runs igni in the
    #: cloud. Said to the person, as an error in their window or on the
    #: command line; it is no part of anything the model reads.
    IN_THE_CLOUD: ClassVar[str] = "Your organisation runs igni for you in the cloud."

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

    def close_for_mode(self, mode: GroupMode) -> bool:
        """Close when ``mode`` keeps igni off this computer; ``True`` when it did.

        Only an igni on the person's own computer closes for ``cloud``
        (``Layer.runs_on_the_persons_computer``): the igni in the cloud
        reads the same pack, and it is where the runs go.
        """
        if mode is not GroupMode.cloud or not layers.current().runs_on_the_persons_computer():
            return False
        self.close(self.IN_THE_CLOUD)
        return True


__all__ = ["GatePhase", "RunGate"]
