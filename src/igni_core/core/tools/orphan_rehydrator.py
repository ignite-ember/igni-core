"""OrphanRehydrator — startup-pass coordinator for orphan pids.

Extracted from :mod:`shell_orphan` per the OOP audit: the previous
``rehydrate_orphan_processes`` was a ~70-LoC procedural coroutine
that took a project_dir first arg, reached for the module-level
supervisor + built a store inside three broad ``except Exception``
blocks that collapsed three failure states into a bare ``int``.

Post-refactor:

* :class:`OrphanRehydrator` — takes a
  :class:`~igni_core.core.tools.process_supervisor.ProcessSupervisor`
  and a :class:`~igni_core.core.tools.process_store.BackgroundProcessStore`
  by constructor injection (composition, not module-level singleton
  reach-in) so tests can wire fake collaborators without touching
  the process-wide supervisor.
* :meth:`OrphanRehydrator.run` — single entry point returning a
  typed :class:`RehydrateResult`. The one failure branch the pass
  can hit (reading the rows) populates ``reason`` so
  :class:`RehydrateController.orphan_processes` can plumb it into
  the :class:`RehydrateOutcome`.
* :class:`RehydrateBuild` — what building the rehydrator produced:
  the rehydrator, or the typed reason there is none.

The store-init failure branch stays on the caller (or the thin
backward-compat wrapper in :mod:`shell_orphan`) because it happens
BEFORE we have a rehydrator.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from igni_core.core.tools.finished_process import FinishedProcess
from igni_core.core.tools.orphan_process import OrphanProcess
from igni_core.core.tools.process_store import BackgroundProcessStore
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.tools.shell_orphan_schemas import RehydrateResult

logger = logging.getLogger(__name__)

#: How long a finished process stays in the history. Seven days is the
#: span over which "what did that build do on Tuesday" is still a
#: question someone asks; past that the row is clutter and the table
#: would grow for the life of the project.
HISTORY_RETENTION_SECONDS = 7 * 24 * 60 * 60


class OrphanRehydrator:
    """Startup-pass coordinator that re-adopts pids the previous
    BE lifetime spawned.

    Reads the persisted background-process rows, probes each pid
    for liveness (via :meth:`OrphanProcess.probe_alive`), injects
    alive orphans into the supervisor's registry as
    :class:`OrphanProcess` instances, surfaces finished ones as
    :class:`FinishedProcess` history, and prunes history older than
    :data:`HISTORY_RETENTION_SECONDS` in the same pass.

    Safe to call multiple times: :meth:`ProcessRegistry.add` is
    idempotent on pid, and pids the registry already tracks are
    skipped so a double-run doesn't double-surface anything.
    """

    def __init__(
        self,
        supervisor: ProcessSupervisor,
        store: BackgroundProcessStore,
    ) -> None:
        self._supervisor = supervisor
        self._store = store

    async def run(self) -> RehydrateResult:
        """Execute the rehydrate pass.

        Returns a typed :class:`RehydrateResult` so the caller
        (:class:`RehydrateController.orphan_processes`) can log
        the surfaced/pruned counts and plumb a failure reason
        into :class:`RehydrateOutcome` instead of swallowing at
        DEBUG.

        The pass is best-effort: a row that cannot be stamped as
        ended is still surfaced, because a process the person
        started did run and did stop. The one failure that stops
        the pass is reading the rows — the store lets that one
        through, and a corrupt state DB must not crash startup.
        """
        self._supervisor.registry.attach_persistence(self._store)

        try:
            rows = await self._store.list_all()
        except Exception as exc:
            logger.debug("orphan rehydrate: list_all failed: %s", exc)
            return RehydrateResult(ok=False, surfaced=0, pruned=0, reason=f"list_all: {exc}")

        surfaced = 0
        for row in rows:
            # Liveness policy lives on :class:`OrphanProcess` so
            # the same probe used inside :meth:`is_running` runs
            # here too — one source of truth.
            if not OrphanProcess.probe_alive(row.pid):
                # A dead pid used to mean "delete the row", which is
                # what made history impossible: the watcher lost every
                # record of what had run the moment the BE restarted.
                #
                # A row that recorded its own ending is history and is
                # surfaced as such. One that did not is a process the
                # previous BE was still watching when it exited — the
                # ending was real but nobody saw it, so it is stamped
                # now with an unknown exit code rather than being
                # quietly dropped or dressed up as a clean exit.
                if not row.is_finished:
                    await self._mark_interrupted(row.pid)
                    row = row.model_copy(update={"finished_at": int(time.time())})
                if self._supervisor.registry.get(row.pid) is None:
                    self._supervisor.registry.add(
                        FinishedProcess.from_row(row, log_store=self._supervisor.log_store)
                    )
                    surfaced += 1
                continue

            # Skip pids the in-process registry already tracks —
            # shouldn't happen on a clean boot (the registry is
            # fresh) but defends against the BE somehow already
            # having spawned the same pid (impossible in practice).
            if self._supervisor.registry.get(row.pid) is not None:
                continue

            orphan = OrphanProcess.from_row(row, log_store=self._supervisor.log_store)
            self._supervisor.registry.add(orphan)
            surfaced += 1

        pruned = await self._prune_expired_history()

        if surfaced:
            logger.info(
                "orphan rehydrate: surfaced %d background process(es) from prior BE lifetime",
                surfaced,
            )

        return RehydrateResult(ok=True, surfaced=surfaced, pruned=pruned)

    async def _prune_expired_history(self) -> int:
        """Drop history rows past the retention window.

        The dead-row prune this replaces ran on liveness, which is why
        history could not exist: every finished process was deleted the
        next time the BE started. Retention is the bound instead — old
        enough, not merely over.
        """
        return int(await self._store.prune_history(older_than_seconds=HISTORY_RETENTION_SECONDS))

    async def _mark_interrupted(self, pid: int) -> None:
        """Stamp a row whose process died without us seeing it.

        ``exit_code=None`` says "it ended, we do not know how" — the
        one honest answer available after the BE was not running to
        observe it. The store reports the failure in its result rather
        than raising, so the row is surfaced either way.
        """
        result = await self._store.finish(pid, None)
        if not result.ok:
            logger.debug("orphan rehydrate: cannot stamp pid=%s: %s", pid, result.reason)


class RehydrateBuild(BaseModel):
    """What building the rehydrator produced.

    ``rehydrator`` is the rehydrator to run, and ``result`` is the
    typed outcome of the build: a failure to build the store is a
    result like any other, with the reason that stopped it.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    rehydrator: OrphanRehydrator | None = None
    result: RehydrateResult = Field(default_factory=RehydrateResult)


def build_rehydrator(
    supervisor: ProcessSupervisor,
    project_dir: str | Path | None,
) -> RehydrateBuild:
    """Construct an :class:`OrphanRehydrator` from a project_dir.

    Kept as a module-level helper (not a classmethod) so the
    constructor stays a plain composition seam. The store-init
    failure comes back as a typed result rather than an exception,
    because it happens before there is a rehydrator to run.
    """
    try:
        store = BackgroundProcessStore(project_dir=project_dir)
    except Exception as exc:
        logger.debug("orphan rehydrate: store init failed: %s", exc)
        return RehydrateBuild(
            result=RehydrateResult(ok=False, surfaced=0, pruned=0, reason=f"store_init: {exc}")
        )
    return RehydrateBuild(rehydrator=OrphanRehydrator(supervisor, store))


__all__ = [
    "HISTORY_RETENTION_SECONDS",
    "OrphanRehydrator",
    "RehydrateBuild",
    "build_rehydrator",
]
