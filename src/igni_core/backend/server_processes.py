"""Background-process watcher RPCs.

Home of :class:`ProcessesController` — the OOP replacement for the
three ``BackendServer``-first-arg free functions that used to live
here (Rule 6 offender). The three wire schemas moved to
:mod:`schemas_processes` per the sibling convention; this module
now holds only the controller.

* :meth:`ProcessesController.list` — cheap "what's running"
  snapshot.
* :meth:`ProcessesController.read_tail` — safe on unknown pids.
* :meth:`ProcessesController.stop` — SIGTERM path.

The controller returns typed :mod:`schemas_processes` models. The
:class:`~igni_core.backend.server.BackendServer` facade methods
(``list_background_processes`` / ``read_process_tail`` /
``stop_background_process``) ``.model_dump()`` at the wire seam so
the JSON-RPC layer keeps seeing the dict shapes it always has —
one dump site, one wire boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from igni_core.backend.schemas_processes import (
    ProcessRow,
    ProcessTailResult,
    StopProcessResult,
)
from igni_core.core.tools.process_supervisor import ProcessSupervisor

# Re-export the wire schemas so existing ``from
# igni_core.backend.server_processes import ProcessRow`` style
# imports (if any surface later) keep working without an update.
__all__ = [
    "ProcessesController",
    "ProcessRow",
    "ProcessTailResult",
    "StopProcessResult",
]


class ProcessesController:
    """Background-process watcher for the process-supervisor
    registry.

    Doesn't hold a session reference because every operation reads
    from a :class:`~igni_core.core.tools.process_supervisor.ProcessSupervisor`
    — the one the backend process shares, injected by the controller
    registry. A test hands it a fresh supervisor for isolation.
    """

    def __init__(self, supervisor: ProcessSupervisor) -> None:
        self._supervisor = supervisor

    def list(self) -> list[ProcessRow]:
        """Every process the registry knows about, running or finished.

        Finished ones are the point: a command that just failed is what
        the panel is opened to read. They live until the registry's
        eviction TTL drops them, so this list is bounded by that.
        """
        return [
            ProcessRow(
                pid=row.pid,
                cmd=row.cmd,
                elapsed_seconds=row.elapsed_seconds,
                is_running=row.is_running,
                exit_code=row.exit_code,
            )
            for row in self._supervisor.registry.all_known()
        ]

    def read_tail(self, pid: int, tail: int = 200) -> ProcessTailResult:
        """Read the last ``tail`` lines from a background process's
        combined stdout/stderr buffer."""
        mp = self._supervisor.registry.get(pid)
        if mp is None:
            return ProcessTailResult(pid=pid, output="", is_running=False, exit_code=None)
        output = mp.read(tail=tail)
        exit_code = mp.returncode() if not mp.is_running() else None
        return ProcessTailResult(
            pid=pid,
            output=output,
            is_running=mp.is_running(),
            exit_code=exit_code,
        )

    #: How long a stopped orphan is given to go before its ending is told.
    ORPHAN_EXIT_WAIT_SECONDS = 2.0

    @classmethod
    async def _until_gone(cls, mp: Any) -> None:
        """Until a signalled orphan has gone, or the wait runs out."""
        deadline = time.monotonic() + cls.ORPHAN_EXIT_WAIT_SECONDS
        while mp.is_running() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    async def stop(self, pid: int) -> StopProcessResult:
        """SIGTERM a background process."""
        mp = self._supervisor.registry.get(pid)
        if mp is None:
            return StopProcessResult(pid=pid, killed=False, message=f"pid {pid} not in registry")
        if not mp.is_running():
            return StopProcessResult(
                pid=pid,
                killed=False,
                message=f"pid {pid} already exited (rc={mp.returncode()})",
            )
        mp.kill()
        # Orphan path: there's no reader task waiting on ``waitpid``
        # to notice the exit + remove from registry + delete the DB
        # row. Do both explicitly.
        #
        # Polymorphic discriminator — both
        # :class:`~igni_core.core.tools.managed_process.ManagedProcess`
        # and :class:`~igni_core.core.tools.orphan_process.OrphanProcess`
        # both declare ``is_orphan`` unconditionally (False / True), so
        # plain attribute access — not ``getattr`` with a default — is
        # the honest duck-check.
        if mp.is_orphan:
            # Its ending is said here, as the reader task says a live
            # one's: the exit event and the history row. It used to be
            # deleted, row and all, so the window never heard it end
            # and the history forgot it ran (campaign R674).
            await self._until_gone(mp)
            self._supervisor.registry.emit_adopted_exit(mp)
            return StopProcessResult(pid=pid, killed=True, message=f"pid {pid} killed")
        # Live-process path — await the reader task so the final
        # tail is flushed before the caller reads back. The task is
        # part of the tracked-process contract, so this asks the
        # process rather than reaching for its private slot.
        reader = mp.reader_task
        if reader is not None:
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(reader, timeout=2.0)
        return StopProcessResult(pid=pid, killed=True, message=f"pid {pid} killed")
