"""ProcessEventBus — single pub/sub API for background-process lifecycle.

Extracted from :mod:`igni_core.core.tools.shell` per
CODE_STANDARDS.md Pattern 8 + Anti-Pattern AP1 (module-level pub/sub).
Before this file, ``shell.py`` had three parallel subscriber APIs —
``subscribe_to_process_start`` / ``subscribe_to_process_line`` /
``subscribe_to_process_completion`` — each with its own list + lock
+ pair of subscribe/unsubscribe functions (12 module-level names
total). This class collapses them into one object with an
``on(event, cb)`` / ``off(event, cb)`` / ``emit(payload)`` interface.

## Events

Payloads are typed :class:`ProcessEvent` union members (see
:mod:`igni_core.core.tools.process_events`). Every event carries its
own ``type`` discriminator and the bus routes on it, so the callback
registered for ``"line"`` is only ever handed a
:class:`ProcessLineEvent`. Concrete types:

* ``"start"`` — :class:`ProcessStartEvent` (``pid``, ``cmd``,
  ``started_at``). Fired once when a backgrounded process is
  registered.
* ``"line"`` — :class:`ProcessLineEvent` (``pid``, ``line``). Fired per
  stdout/stderr line. Hot path.
* ``"exit"`` — :class:`ProcessExitEvent` (``pid``, ``cmd``,
  ``exit_code``, ``duration_seconds``, ``output_tail``). Fired once
  when the process exits.

## Semantics

- Subscribe is idempotent — registering the same callback twice
  leaves ONE registration.
- Emit is fail-soft — a subscriber that raises is logged and the
  next subscriber still fires.
- Emit reads the subscriber list under the lock, then releases
  before firing — no lock held while a subscriber runs.

## Thread safety

Lock-guarded. Called from both the async event loop (reader tasks,
completion emits) and from FE-facing callbacks that may run on a
different thread. Acquisition is contested rarely enough that a
single ``threading.Lock`` beats per-event locks on cache footprint.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from typing import Literal, overload

from igni_core.core.tools.process_events import (
    EventType,
    ExitSubscriber,
    LineSubscriber,
    ProcessEvent,
    ProcessEventCallback,
    StartSubscriber,
)

logger = logging.getLogger(__name__)

#: Every event the bus routes on, in registration order.
EVENT_TYPES: tuple[EventType, ...] = ("start", "line", "exit")


class ProcessEventBus:
    """Fan-out subscriber bus for background-process lifecycle events.

    Instantiate once, share across the module. See module docstring
    for the event set + semantics.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[EventType, list[ProcessEventCallback]] = {e: [] for e in EVENT_TYPES}

    @overload
    def on(self, event: Literal["start"], cb: StartSubscriber) -> None: ...

    @overload
    def on(self, event: Literal["line"], cb: LineSubscriber) -> None: ...

    @overload
    def on(self, event: Literal["exit"], cb: ExitSubscriber) -> None: ...

    def on(self, event: EventType, cb: Callable[..., None]) -> None:
        """Register ``cb`` for ``event``. Idempotent — registering
        the same callback twice results in one registration.

        The overloads above are the contract; the implementation
        signature is deliberately permissive, because the bus routes
        on the payload's own ``type`` discriminator — a subscriber
        registered for one event only ever sees that event's model.
        """
        if event not in self._subs:
            raise ValueError(f"unknown event type: {event!r}")
        with self._lock:
            subscribers = self._subs[event]
            if cb not in subscribers:
                subscribers.append(cb)

    @overload
    def off(self, event: Literal["start"], cb: StartSubscriber) -> None: ...

    @overload
    def off(self, event: Literal["line"], cb: LineSubscriber) -> None: ...

    @overload
    def off(self, event: Literal["exit"], cb: ExitSubscriber) -> None: ...

    def off(self, event: EventType, cb: Callable[..., None]) -> None:
        """Unregister ``cb`` from ``event``. No-op if not registered."""
        if event not in self._subs:
            return
        with self._lock, contextlib.suppress(ValueError):
            self._subs[event].remove(cb)

    def emit(self, payload: ProcessEvent) -> None:
        """Fire every subscriber for the event ``payload`` names.

        The payload's own ``type`` discriminator is the route — there
        is no second event name to keep in sync, so a
        ``ProcessExitEvent`` can never be announced as a "line".

        Snapshots the subscriber list under the lock, then releases
        before firing. A subscriber that raises is logged and the
        next subscriber still runs.
        """
        event = payload.type
        with self._lock:
            registered = self._subs.get(event)
            subscribers = list(registered) if registered is not None else []
        if registered is None:
            # A payload whose ``type`` the bus does not route is a bug
            # in the emitter, not a subscriber's fault — say so and
            # drop it rather than raising inside the reader task.
            logger.warning("emit for unknown event type %r — dropping payload", event)
            return
        for cb in subscribers:
            try:
                cb(payload)
            except Exception as exc:
                logger.warning("%s subscriber raised: %s", event, exc)

    def subscriber_count(self, event: EventType) -> int:
        """Number of registered subscribers for ``event``. Test-only
        helper — production code doesn't need to introspect."""
        if event not in self._subs:
            return 0
        with self._lock:
            return len(self._subs[event])

    def reset(self) -> None:
        """Drop every subscriber. Test-fixture helper — session teardown
        doesn't need to call this; the bus is process-lifetime."""
        with self._lock:
            for e in EVENT_TYPES:
                self._subs[e].clear()
