"""Session-level broadcast machinery.

:class:`BroadcastBus` owns the push-channel fan-out — a callback
list plus a deferred-until-run-finish queue. Session composes
one instance (``self.broadcast_bus = BroadcastBus()``); the two
lists live inside the bus, not on Session.

Delivery contract:

* :meth:`BroadcastBus.emit` hands the typed event to every
  subscriber synchronously; every subscriber receives the same
  frozen instance. Subscribers are expected to just enqueue a
  push, not to await IO. Exceptions in one subscriber don't sink
  the rest — each is guarded independently.
* :meth:`BroadcastBus.queue_post_run` defers delivery until
  the stream consumer calls :meth:`drain_post_run` (right
  after ``RunCompleted`` / ``RunError``). Used by tools whose
  card must appear at the *bottom* of a reply, not mid-stream
  — most notably ``exit_plan_mode``'s PlanCard.
"""

from __future__ import annotations

import logging

from igni_core.core.broadcast_schema import BroadcastEvent, BroadcastSink

logger = logging.getLogger(__name__)


__all__ = ["BroadcastBus"]


class BroadcastBus:
    """Owns the callback list and the post-run deferral queue.

    Session composes one of these in its ``__init__``; every
    subsystem that needs to push (plan-mode approval, output-style
    swap, permission-mode change, …) calls into the instance
    methods instead of poking module-level state.

    Test fixtures that stub Session via ``Session.__new__`` MUST
    also set ``session.broadcast_bus = BroadcastBus()`` — the
    bus never returns a no-op default when unset; callers get
    ``AttributeError`` and the missing wire-up surfaces at test
    time.
    """

    def __init__(self) -> None:
        self._callbacks: list[BroadcastSink] = []
        self._pending: list[BroadcastEvent] = []

    # ── Callback registration ──────────────────────────────────

    def register(self, subscriber: BroadcastSink) -> None:
        """Append ``subscriber`` to the subscriber list. Idempotent — registering the same callable twice
        does not double-fire (load-bearing for
        ``test_session::test_register_is_idempotent_on_same_callback``:
        both ``/plan`` and ``/accept`` slash-command bootstrap
        can register the same transport shim without inflating
        fan-out)."""
        if subscriber not in self._callbacks:
            self._callbacks.append(subscriber)

    # ── Read-only accessors ────────────────────────────────────
    # Public reads for state callers used to inspect on the raw
    # lists — kept as accessors so the private list identity
    # stays owned by the bus.

    @property
    def has_callbacks(self) -> bool:
        """True when at least one callback is registered."""
        return bool(self._callbacks)

    @property
    def pending_count(self) -> int:
        """Number of events queued for post-run drain."""
        return len(self._pending)

    def callbacks_snapshot(self) -> list[BroadcastSink]:
        """Copy of the callback list — safe to iterate without
        mutating the live subscription set."""
        return list(self._callbacks)

    # ── Immediate delivery ─────────────────────────────────────

    def emit(self, event: BroadcastEvent) -> None:
        """Hand ``event`` to every registered subscriber.
        Synchronous — subscribers enqueue, they don't await.

        The callback list is iterated via ``list(self._callbacks)``
        so a callback that ``register()``s during broadcast joins
        the NEXT emit, not the current one — mutating a list
        during iteration would otherwise raise RuntimeError.
        """
        for cb in list(self._callbacks):
            try:
                cb(event)
            except Exception as exc:
                logger.debug(
                    "broadcast callback raised on channel %s: %s",
                    event.channel,
                    exc,
                )

    # ── Deferred delivery ──────────────────────────────────────

    def queue_post_run(self, event: BroadcastEvent) -> None:
        """Enqueue ``event`` for delivery after the current run
        wraps up.

        Used by tools whose result is meant to render *after* all
        the agent's content — most notably ``exit_plan_mode``. If
        we broadcast inline, the PlanCard appears mid-stream,
        above whatever closing message the agent emits.
        """
        self._pending.append(event)

    def drain_post_run(self, run_id: str | None = None) -> None:
        """Fire every queued event through :meth:`emit`, then
        clear the queue.

        Called by the BE stream consumer right after
        ``RunCompleted`` flushes. Safe on an empty queue (no-op).

        ``run_id`` (when supplied) is offered to each event via
        :meth:`BroadcastEvent.with_run_id`; run-scoped events
        (``plan_submitted``) take it, since the tool that queued
        them cannot see the current run_id.
        """
        if not self._pending:
            return
        # Snapshot + clear so a callback that re-queues doesn't
        # loop in the same drain pass — the re-queued entry
        # survives to the NEXT drain.
        pending = list(self._pending)
        self._pending.clear()
        for event in pending:
            self.emit(event.with_run_id(run_id))
