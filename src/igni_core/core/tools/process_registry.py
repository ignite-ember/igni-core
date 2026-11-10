"""ProcessRegistry — lifecycle owner for background-shell processes.

Split out of :mod:`process_supervisor` when the audit flagged the
supervisor as a data-behaviour god: the registry was a passive
dict guarded by a lock while the *coordination* logic (announce
start + persist, remove + persist_remove + cancel eviction, arm
eviction task, emit line, emit completion) sat on
:class:`ProcessSupervisor` operating on the registry from outside.

This module gives that behaviour a home. The registry is the
coordinator; the concerns it coordinates live in collaborators it
holds rather than in fields of its own:

* :class:`~igni_core.core.tools.process_eviction.ProcessEviction` —
  the finished-process TTL and the per-pid eviction tasks.
* :class:`~igni_core.core.tools.process_persistence.ProcessPersistenceWriter`
  — the fire-and-forget writes of the registry's rows to the store.
* :class:`~igni_core.core.tools.process_registry_schemas.RegistryCollaborators`
  — the event bus, the on-disk log store and the sleep blocker.

The processes themselves are held as
:class:`~igni_core.core.tools.process_registry_schemas.TrackedProcess`
— the contract the live, orphaned and finished kinds all satisfy —
so the public API says what it holds instead of saying ``Any``.

Public collaborators the supervisor still needs (bus, log store,
persistence configuration) are exposed as attributes / methods so
the small handful of sync-import call sites can reach them via
``supervisor.registry.bus`` etc.

Threading model:

* :class:`ProcessRegistry` is sync-lock guarded — safe from both
  async tools and the sync ``cancel_foreground`` path.
* :class:`ProcessEventBus` (owned as ``self.bus``) is also
  sync-lock guarded.
* Eviction task handles live on the registry's
  :class:`~igni_core.core.tools.process_eviction.ProcessEviction` —
  previously reached in via a private attr on
  :class:`ManagedProcess`, which broke encapsulation the audit
  called out.
"""

from __future__ import annotations

import logging
import threading
import time

from igni_core.core.tools.process_bus import ProcessEventBus
from igni_core.core.tools.process_events import (
    ProcessExitEvent,
    ProcessLineEvent,
    ProcessStartEvent,
)
from igni_core.core.tools.process_eviction import ProcessEviction
from igni_core.core.tools.process_log import ProcessLogStore
from igni_core.core.tools.process_persistence import ProcessPersistenceWriter
from igni_core.core.tools.process_registry_schemas import (
    BackgroundProcessPersistence,
    KnownProcess,
    RegistryCollaborators,
    RunningProcess,
    TrackedProcess,
)
from igni_core.core.tools.sleep_blocker import SleepBlocker

logger = logging.getLogger(__name__)

#: Default finished-process eviction TTL (10 minutes after the
#: most recent read).
DEFAULT_FINISHED_PROCESS_TTL_SECONDS = 600.0


class ProcessRegistry:
    """Owning collaborator for every backgrounded shell process.

    Holds the pid → process map, the event bus, the on-disk log
    store, and the optional persistence store. Announce / deregister /
    completion methods co-locate the emit + persist steps with the
    registry mutation so callers can't accidentally skip half the
    lifecycle.

    Constructor collaborators are all optional so tests can build
    a bare ``ProcessRegistry()`` and get sensible defaults (fresh
    bus, log store with ``project_dir=None``, no persistence).
    Production wiring passes real instances via
    :meth:`attach_persistence` (persistence) and
    :meth:`ProcessSupervisor.configure_log_store` (log store root).
    """

    def __init__(
        self,
        bus: ProcessEventBus | None = None,
        log_store: ProcessLogStore | None = None,
        persistence: BackgroundProcessPersistence | None = None,
        ttl_seconds: float = DEFAULT_FINISHED_PROCESS_TTL_SECONDS,
        sleep_blocker: SleepBlocker | None = None,
    ) -> None:
        self._processes: dict[int, TrackedProcess] = {}
        self._lock = threading.Lock()
        self._collaborators = RegistryCollaborators(
            bus=bus or ProcessEventBus(),
            log_store=log_store or ProcessLogStore(None),
            sleep_blocker=sleep_blocker if sleep_blocker is not None else SleepBlocker(),
        )
        self._eviction = ProcessEviction(ttl_seconds)
        self._persistence = ProcessPersistenceWriter(persistence)

    # ── Collaborators ───────────────────────────────────────────

    @property
    def bus(self) -> ProcessEventBus:
        """The event bus the registry announces on."""
        return self._collaborators.bus

    @property
    def log_store(self) -> ProcessLogStore:
        """The per-pid on-disk log store."""
        return self._collaborators.log_store

    # ── Persistence wiring ──────────────────────────────────────

    def attach_persistence(self, store: BackgroundProcessPersistence | None) -> None:
        """Point this registry at a
        :class:`~igni_core.core.tools.process_store.BackgroundProcessStore`
        (or ``None`` to unwire for test isolation). Called from
        :meth:`~igni_core.core.tools.orphan_rehydrator.OrphanRehydrator.run`
        during BE startup once the DB is live."""
        self._persistence.attach(store)

    @property
    def persistence(self) -> BackgroundProcessPersistence | None:
        """Currently-attached persistence store, or ``None``."""
        return self._persistence.store

    # ── TTL knob ────────────────────────────────────────────────

    def set_ttl_seconds(self, seconds: float) -> None:
        """Override the finished-process eviction TTL. Value is
        read fresh on every :meth:`arm_eviction` call so the
        change takes effect on subsequent arms."""
        self._eviction.set_ttl_seconds(seconds)

    @property
    def ttl_seconds(self) -> float:
        """Current finished-process eviction TTL."""
        return self._eviction.ttl_seconds

    # ── Add / remove ────────────────────────────────────────────

    def add(self, mp: TrackedProcess) -> int:
        """Add ``mp`` to the pid map WITHOUT firing subscribers or
        persisting.

        The shell tool's spawn path uses this to make the process
        readable from :meth:`get` immediately, then calls
        :meth:`announce_start` once the caller has decided the
        process is worth surfacing (backgrounded, or foreground
        that timed out and got auto-promoted). Also used by
        orphan rehydration, where we DON'T want to re-emit start
        for a process that already exists.
        """
        pid = mp.proc.pid
        with self._lock:
            self._processes[pid] = mp
        return pid

    def announce_start(self, mp: TrackedProcess) -> None:
        """Emit the ``start`` event AND persist the DB row.

        Called from the two paths that surface a process to the
        watcher: backgrounded spawn, and foreground-that-timed-out
        auto-promotion. Persist FIRST so a subscriber that crashes
        can't sink the orphan-recovery DB row (subscribers are FE
        pushes; the DB row is load-bearing for cross-restart
        tracking).
        """
        self._persistence.record_start(mp.proc.pid, mp.cmd)
        # Paired with the release in ``emit_completion``. Taken here
        # rather than in ``add`` because this is the point at which the
        # process is something the agent is waiting on, not merely a
        # pid that exists.
        self._collaborators.sleep_blocker.acquire()
        self.bus.emit(
            ProcessStartEvent(pid=mp.proc.pid, cmd=mp.cmd, started_at=time.time()),
        )

    def remove(self, pid: int) -> None:
        """Remove ``pid`` from the pid map. Also delete the
        persisted row and cancel any pending eviction task —
        this is the single "tear down for this pid" entry point.

        Idempotent: extra calls no-op.
        """
        self._persistence.forget(pid)
        with self._lock:
            self._processes.pop(pid, None)
        self._eviction.cancel(pid)

    def record_finished(self, mp: TrackedProcess) -> None:
        """Persist the ending of a process that was never announced.

        ``emit_completion`` is gated on ``was_backgrounded`` — the exit
        push and its queue notice are for work the agent handed off,
        not for every ``$ ls``. But history is for every ``$ ls``: the
        row is the only record that the command ran at all, and without
        this the quick ones existed in memory until the BE stopped and
        nowhere afterwards.

        No bus event, deliberately. Nobody is waiting on a foreground
        command they already have the output of.
        """
        self._persistence.record_end(mp)
        self.arm_eviction(mp)

    def emit_completion(self, mp: TrackedProcess) -> None:
        """Fire ``exit`` subscribers AND record the ending.

        Called from the reader task once stdout closes.

        Records the ending rather than deleting the row: the table is
        a history now, and "what ran here and how did it go" is the
        question the watcher asks after a restart.
        """
        self._persistence.record_end(mp)
        self._collaborators.sleep_blocker.release()
        self.bus.emit(
            ProcessExitEvent(
                pid=mp.proc.pid,
                cmd=mp.cmd,
                exit_code=mp.proc.returncode,
                duration_seconds=mp.elapsed(),
                output_tail=mp.read(tail=40),
            ),
        )

    def emit_line(self, pid: int, line: str) -> None:
        """Notify ``line`` subscribers. Hot path — the bus
        snapshots the subscriber list under its own lock, so this
        stays contention-free at the callsite."""
        self.bus.emit(ProcessLineEvent(pid=pid, line=line))

    # ── Query ───────────────────────────────────────────────────

    def get(self, pid: int) -> TrackedProcess | None:
        with self._lock:
            return self._processes.get(pid)

    def all_running(self) -> list[RunningProcess]:
        """Every running process, with how long it has been up.

        Uses polymorphic ``mp.elapsed()`` — both
        :class:`ManagedProcess` (monotonic) and
        :class:`~igni_core.core.tools.orphan_process.OrphanProcess`
        (epoch) implement it, no duck-check needed here."""
        with self._lock:
            return [
                RunningProcess(pid=pid, cmd=mp.cmd, elapsed_seconds=mp.elapsed())
                for pid, mp in self._processes.items()
                if mp.is_running()
            ]

    def all_known(self) -> list[KnownProcess]:
        """Every process the registry holds — finished ones included.

        :meth:`all_running` answers "what is running now", which is the
        wrong question for the watcher: a command that failed four
        seconds ago is exactly what someone opens the panel to read, and
        filtering it out meant the row vanished at the moment it became
        interesting. Finished entries stay until their eviction TTL
        expires, so this is bounded by that rather than unbounded.
        """
        with self._lock:
            rows: list[KnownProcess] = []
            for pid, mp in self._processes.items():
                running = mp.is_running()
                rows.append(
                    KnownProcess(
                        pid=pid,
                        cmd=mp.cmd,
                        elapsed_seconds=mp.elapsed(),
                        is_running=running,
                        exit_code=None if running else mp.returncode(),
                    )
                )
            return rows

    def clear(self) -> None:
        """Drop every entry + every pending eviction task. Test-
        isolation helper."""
        with self._lock:
            self._processes.clear()
        self._eviction.cancel_all()

    def kill_all(self) -> int:
        """Kill all tracked processes synchronously. Returns count
        killed. Used at BE shutdown — we just send SIGKILL and
        don't wait for the processes to fully reap; the async
        loop is already torn down so we can't ``await proc.wait()``."""
        with self._lock:
            count = 0
            for mp in self._processes.values():
                if mp.is_running():
                    mp.kill()
                    count += 1
            self._processes.clear()
        self._eviction.cancel_all()
        return count

    # ── Eviction ────────────────────────────────────────────────

    def arm_eviction(self, mp: TrackedProcess) -> None:
        """Arm/refresh the async eviction task for a finished
        process. Called from ``read_process_output`` whenever the
        agent reads a finished process; cancels any existing task
        first so the TTL resets to "10 min after the *most recent*
        read", not "10 min after the first read"."""
        pid = mp.proc.pid
        self._eviction.arm(pid, lambda: self._expire(pid, mp))

    def _expire(self, pid: int, mp: TrackedProcess) -> None:
        """Drop a finished process whose TTL has run out.

        Skipped if the pid now holds a different process — a reused
        pid must not evict the process that took its place.
        """
        with self._lock:
            current = self._processes.get(pid)
            if current is not mp:
                return
            self._processes.pop(pid, None)
        self._eviction.forget(pid)
        logger.debug("evicted process pid=%d after TTL", pid)
        # Per-pid log file follows the same TTL — the agent's
        # in-memory buffer and the on-disk tail-able copy should go
        # away together.
        self.log_store.cleanup(pid)

    # ── Test / reset helpers ────────────────────────────────────

    def reset(self) -> None:
        """Drop subscribers + registry entries + eviction tasks +
        persistence handle. Fixture-teardown helper — production
        BE never calls this."""
        self.bus.reset()
        self.clear()
        self._persistence.detach()
