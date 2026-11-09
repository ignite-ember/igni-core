"""The supervisor one backend process shares.

One backend process owns exactly one
:class:`~igni_core.core.tools.process_supervisor.ProcessSupervisor`:
the process registry, the event bus and the foreground slot are
process-wide — the watcher panel lists every background command the
process started, whichever session started it.

:class:`SupervisorLocator` is where that shared instance lives. The
backend's composition root builds one per backend process and injects
it into the two things that outlive a single session — the
:class:`~igni_core.backend.server.BackendServer` and the pool's
:class:`~igni_core.backend.pool_wiring.PoolWiring` — which hand the
supervisor itself to the session, the controllers and the toolkits
that need it.

There is no module-level instance and no mutable slot to swap, so a
second backend process, a pooled session and a test each start from
their own locator instead of reaching into a global.
"""

from __future__ import annotations

from igni_core.core.tools.process_supervisor import ProcessSupervisor


class SupervisorLocator:
    """Holds the :class:`ProcessSupervisor` one backend process shares.

    Construct one per backend process, at the composition root, and
    inject it where the shared supervisor is needed. The supervisor is
    built eagerly by the constructor — a wiring failure surfaces where
    the backend is assembled, not on the first command that needs a
    process — and it is never swapped afterwards, so nothing that
    holds a locator can end up talking to a different supervisor than
    the one it started with.

    Tests that want an isolated supervisor build their own locator
    rather than resetting a shared one.
    """

    def __init__(self, supervisor: ProcessSupervisor | None = None) -> None:
        # Passed in when the process already has one (the composition
        # root builds the shared supervisor and hands it over);
        # otherwise this locator owns a fresh one.
        self._supervisor = supervisor if supervisor is not None else ProcessSupervisor()

    @property
    def supervisor(self) -> ProcessSupervisor:
        """The supervisor this backend process shares."""
        return self._supervisor
