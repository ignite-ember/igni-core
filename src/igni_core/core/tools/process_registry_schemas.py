"""Typed contracts for the background-process registry.

The registry holds three kinds of thing and used to spell all three as
``Any``: the processes it tracks, the store it persists rows through,
and the rows it hands back to the watcher. This module gives each one
a name, so the registry's public API says what it holds.

* :class:`TrackedProcess` — what the registry reads off a process it
  tracks. Three kinds satisfy it:
  :class:`~igni_core.core.tools.managed_process.ManagedProcess` (a
  live subprocess this BE spawned),
  :class:`~igni_core.core.tools.orphan_process.OrphanProcess` (one that
  outlived its BE) and
  :class:`~igni_core.core.tools.finished_process.FinishedProcess` (one
  that ended and was read back from disk).
* :class:`BackgroundProcessPersistence` — the store seam the registry
  writes its rows through. The concrete
  :class:`~igni_core.core.tools.process_store.BackgroundProcessStore`
  satisfies it.
* :class:`RunningProcess` / :class:`KnownProcess` — the row shapes
  the watcher's list is made of. They were positional tuples, so a
  caller had to know the order to read a field out of one.
* :class:`RegistryCollaborators` — the three collaborators the
  registry emits through, bundled so the registry itself holds a
  small number of instance fields.

Protocols, not base classes: the three process kinds share this
surface and nothing else, and a base class would mean editing all
three every time a shared method changed.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from igni_core.core.tools.output_tail import OutputTail
from igni_core.core.tools.process_bus import ProcessEventBus
from igni_core.core.tools.process_log import ProcessLogStore
from igni_core.core.tools.process_store_schemas import (
    BackgroundProcessRow,
    RemoveResult,
    UpsertResult,
)
from igni_core.core.tools.sleep_blocker import SleepBlocker


class ProcessHandle(Protocol):
    """The two fields of a process handle the registry reads.

    A live process carries a real
    :class:`asyncio.subprocess.Process`; an orphan and a finished
    process carry a two-field stub. Only ``pid`` and ``returncode``
    are common, so only those two are in the contract.
    """

    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...


class TrackedProcess(Protocol):
    """The surface the registry and the watcher read off a tracked process.

    Every member is read-only on purpose: the registry observes a
    process, it does not steer it. The three implementations differ in
    how they answer (``elapsed`` is monotonic for a live process and
    wall-clock for one that outlived its BE) but they answer the same
    questions.
    """

    @property
    def proc(self) -> ProcessHandle: ...

    @property
    def cmd(self) -> str: ...

    @property
    def is_orphan(self) -> bool: ...

    @property
    def started_epoch(self) -> int:
        """When the process started, in epoch seconds.

        Wall-clock rather than monotonic on purpose: the value is
        written to a row a later BE lifetime reads back, and that
        process's monotonic clock means nothing to this one.
        """

    @property
    def reader_task(self) -> asyncio.Task[Any] | None:
        """The task draining the process's stdout, when it has one.

        ``None`` for a process this BE cannot read any more — an
        orphan from a previous lifetime, or a finished row.
        """

    def is_running(self) -> bool: ...

    def returncode(self) -> int | None: ...

    def elapsed(self) -> float: ...

    def read(self, tail: int = 100) -> str: ...

    def read_tail(self, tail: int = 100) -> OutputTail: ...

    def read_new(self, max_lines: int = 200) -> str: ...

    def kill(self) -> None: ...


class BackgroundProcessPersistence(Protocol):
    """The store seam the registry writes its rows through.

    Mirrors :class:`~igni_core.core.tools.process_store.BackgroundProcessStore`
    — the concrete store satisfies it — and lets a test double stand in
    for one without the registry falling back to ``Any``. Every method
    reports failure in its typed result rather than raising, so a
    database that is down cannot take a spawn path down with it.
    """

    async def upsert(self, row: BackgroundProcessRow) -> UpsertResult: ...

    async def remove(self, pid: int) -> RemoveResult: ...

    async def finish(
        self,
        pid: int,
        exit_code: int | None,
        *,
        row: BackgroundProcessRow | None = None,
    ) -> UpsertResult: ...


class RunningProcess(BaseModel):
    """One running process, as the registry reports it."""

    pid: int
    cmd: str
    elapsed_seconds: float


class KnownProcess(BaseModel):
    """One process the registry knows about — running or finished.

    Finished entries are the point of the model: a command that failed
    four seconds ago is what someone opens the panel to read.
    """

    pid: int
    cmd: str
    elapsed_seconds: float
    is_running: bool
    exit_code: int | None = None


class RegistryCollaborators(BaseModel):
    """The collaborators the registry emits through and writes with.

    Bundled so the registry holds a handful of fields rather than one
    per collaborator. Frozen: these are wired once and then read.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    bus: ProcessEventBus
    log_store: ProcessLogStore
    sleep_blocker: SleepBlocker
