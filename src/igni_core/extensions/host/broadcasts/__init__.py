"""What igni does, as host-model signals.

Two sources, which together are every change igni makes:

* **every message igni sends its windows** — run events, mirroring, the
  push channels. :meth:`BroadcastSignals.observe` sees each one on its
  way out (the backend's transport is wrapped once, at the root) and
  translates it into the model's signal, in the model's shape;
* **every write an RPC makes**, whoever made it — a window or an
  extension. :meth:`BroadcastSignals.after_rpc` is told after each
  successful one (:class:`RpcWrite`), so "every write is a signal" holds
  for writes that broadcast nothing of their own.

The translation is the point of this package. igni's broadcasts are its
private wire and change with its UI; the :class:`~.signal.Signal` models
here are the promise — each one the host schema's type for its wire,
field for field. A field renamed on the wire is fixed in one model, once,
and no extension notices.

This module is the composition root: each translator is built with only
the collaborators it needs, and their message tables are merged into one.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, ClassVar

from igni_core.core.approvals.events import ApprovalChange
from igni_core.extensions.host.broadcasts.approval_signals import ApprovalSignals
from igni_core.extensions.host.broadcasts.chat_signals import ChatSignals
from igni_core.extensions.host.broadcasts.deferred_reads import DeferredReads, SessionReaders
from igni_core.extensions.host.broadcasts.project_paths import ProjectRoot
from igni_core.extensions.host.broadcasts.push_signals import PushSignals
from igni_core.extensions.host.broadcasts.rpc_writes import (
    ExtensionSettings,
    RpcWrite,
    RpcWriteSignals,
)
from igni_core.extensions.host.broadcasts.run_signals import RunSignals
from igni_core.extensions.host.broadcasts.signal import Handler, merged
from igni_core.extensions.host.broadcasts.status import StatusSignals
from igni_core.extensions.host.broadcasts.task_signals import TaskSignals
from igni_core.extensions.host.broadcasts.views import ViewShown
from igni_core.extensions.host.signal_values import Emitter
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod
from igni_core.protocol.schemas.enums import PushChannel

if TYPE_CHECKING:
    # A genuine cycle: the services are built from this package's parts
    # (ActivityWatch, the approval mirror, the run ledger).
    from igni_core.extensions.host.services import HostServices

logger = logging.getLogger(__name__)


class BroadcastSignals:
    """Routes igni's traffic to the translator that owns each kind of it."""

    #: Messages after which igni may have started or stopped doing something.
    ACTIVITY_MOVES: ClassVar[frozenset[type[msg.Message]]] = frozenset(
        {
            msg.RunStarted,
            msg.RunCompleted,
            msg.RunError,
            msg.RunPaused,
            msg.HITLRequest,
            msg.RequirementResolved,
            msg.StreamEnd,
            msg.StreamingDone,
        }
    )

    def __init__(self, host: HostServices, *, display: Callable[[str], Awaitable[Any]]) -> None:
        """
        Args:
            display: Reads ``app.display`` for a session, as
                ``AppMembers.display_value`` does.
        """
        emitter = Emitter(host.bus.emit)
        state = host.state
        backend = host.backend
        self._activity = host.activity_watch
        self.approvals = ApprovalSignals(emitter, state.approvals, self._activity)
        reads = DeferredReads(
            host.bus.wanted,
            host.work.spawn,
            emitter,
            SessionReaders(
                account=lambda sid: host.gateway.account(sid),
                display=display,
                loop=lambda sid: backend.rpc(RpcMethod.LOOP_STATUS, {}, session_id=sid),
                default_session=lambda: backend.default_session(),
            ),
        )
        tasks = TaskSignals(emitter)
        root = ProjectRoot(
            lambda sid: backend.project_dir_of(sid), lambda: backend.default_session()
        )
        runs = RunSignals(emitter, state.runs, state.approvals, root)
        self._push = PushSignals(emitter, runs, tasks, reads)
        self._rpc = RpcWriteSignals(
            emitter,
            reads,
            tasks,
            ExtensionSettings(host.extensions.registry, host.extensions.store),
            host.observations.session_switched,
        )
        self._handlers: dict[type[msg.Message], Handler] = merged(
            ChatSignals(emitter, host.windows).handlers(),
            runs.handlers(),
            self.approvals.handlers(),
            StatusSignals(emitter, state.status).handlers(),
            tasks.handlers(),
            {msg.PushNotification: self._push.push},
        )

    def outbound(self, message: msg.Message) -> None:
        """Every message igni sends its windows; a translator's failure is logged, not raised."""
        try:
            self.observe(message)
        except Exception:
            logger.exception("host model: observing %s failed", getattr(message, "type", "?"))

    def observe(self, m: msg.Message) -> None:
        """One message igni sends its windows."""
        handler = self._handlers.get(type(m))
        if handler is not None:
            handler(m)
        if type(m) in self.ACTIVITY_MOVES:
            self._activity.moved()

    def after_rpc(self, write: RpcWrite) -> None:
        """A write succeeded — whoever made it."""
        self._rpc.handle(write)

    def approval_changed(self, change: ApprovalChange) -> None:
        """An approval appeared or went away (``core/approvals/events.py``)."""
        self.approvals.changed(change)

    def handled(self) -> frozenset[type[msg.Message]]:
        """Every message class translated, for the coverage test."""
        return frozenset(self._handlers)

    def push_channels(self) -> frozenset[PushChannel]:
        """Every push channel translated, for the coverage test."""
        return self._push.channels()


__all__ = ["BroadcastSignals", "RpcWrite", "ViewShown"]
