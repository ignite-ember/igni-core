"""Fire-and-forget persistence of the registry's background-process rows.

Split out of :mod:`igni_core.core.tools.process_registry`: writing a
row is its own concern, with its own collaborator (the store) and its
own scheduling rule (never in front of the caller). The registry
decides *when* a row changes; this class decides how that change
reaches the database.

Every write here is scheduled, not awaited. The registry's callers
are synchronous — the shell tool's spawn path, a reader task, a
shutdown hook — and a database write must not sit in front of them.
The store answers with a typed result rather than raising, so a
failed write is logged with its reason instead of surfacing as an
exception in a caller that cannot do anything about it.
"""

from __future__ import annotations

import logging

from igni_core.core.tools.async_fire_and_forget import AsyncFireAndForget
from igni_core.core.tools.managed_process import process_group_of
from igni_core.core.tools.process_registry_schemas import (
    BackgroundProcessPersistence,
    TrackedProcess,
)
from igni_core.core.tools.process_store_schemas import BackgroundProcessRow

logger = logging.getLogger(__name__)


class ProcessPersistenceWriter:
    """Writes the registry's rows to the store without blocking.

    One instance per registry. The store handle is attached at
    construction or later (the backend attaches it once the database
    is live); until then every write is a no-op, which is what lets
    the registry exist before the database does.
    """

    def __init__(self, store: BackgroundProcessPersistence | None = None) -> None:
        self._store = store
        self._scheduler = AsyncFireAndForget()

    @property
    def store(self) -> BackgroundProcessPersistence | None:
        """The attached store, or ``None`` when persistence is off."""
        return self._store

    def attach(self, store: BackgroundProcessPersistence | None) -> None:
        """Point the writer at a store (``None`` unwires it)."""
        self._store = store

    def detach(self) -> None:
        """Unwire the store — the test-isolation path."""
        self._store = None

    def record_start(self, pid: int, cmd: str) -> None:
        """Write the row for a process that has just been announced.

        ``os.getpgid`` does not exist on Windows, so asking for the
        name and returning ``None`` where there are no process groups
        is the honest answer: the row then carries ``pgid=None``.
        """
        store = self._store
        if store is None:
            return
        row = BackgroundProcessRow.new(pid=pid, cmd=cmd, pgid=process_group_of(pid))
        self._scheduler.schedule(self._await_upsert(store, row))

    def record_end(self, mp: TrackedProcess) -> None:
        """Write how a process ended, keeping its row as history.

        The row stays; it gains an exit code and a timestamp, which is
        what makes the table survivable as history rather than a list
        of what happens to be alive.
        """
        store = self._store
        if store is None:
            return
        self._scheduler.schedule(self._await_finish(store, mp))

    def forget(self, pid: int) -> None:
        """Delete the row for a pid that is being torn down."""
        store = self._store
        if store is None:
            return
        self._scheduler.schedule(self._await_remove(store, pid))

    async def _await_upsert(
        self,
        store: BackgroundProcessPersistence,
        row: BackgroundProcessRow,
    ) -> None:
        """Await ``store.upsert`` and log the reason on failure."""
        result = await store.upsert(row)
        if not result.ok:
            logger.debug("persist_add failed: %s", result.reason)

    async def _await_finish(
        self,
        store: BackgroundProcessPersistence,
        mp: TrackedProcess,
    ) -> None:
        """Await ``store.finish`` and log the reason on failure.

        The row is built here, where the command and the start time
        are still to hand. A process that was never announced has no
        row yet, so the ending is written whole — command, start,
        exit code — which is what makes a one-shot survive a restart
        instead of existing only in memory until the backend stops.
        """
        pid = mp.proc.pid
        row = BackgroundProcessRow(pid=pid, cmd=mp.cmd, pgid=None, started_at=mp.started_epoch)
        result = await store.finish(pid, mp.proc.returncode, row=row)
        if not result.ok:
            logger.debug("persist_finish pid=%s failed: %s", pid, result.reason)

    async def _await_remove(
        self,
        store: BackgroundProcessPersistence,
        pid: int,
    ) -> None:
        """Await ``store.remove`` and log the reason on failure."""
        result = await store.remove(pid)
        if not result.ok:
            logger.debug("persist_remove pid=%s failed: %s", pid, result.reason)
