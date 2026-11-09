"""Cross-thread push-notification bridge.

The BE's tools + subscribers can fire from arbitrary threads: the
shell reader thread, the file-edit listener thread, the orchestrate
progress callback. ``transport.send`` is async and must run on the
event loop. :class:`PushNotificationBridge` centralises the
loop-affinity + coroutine-scheduling boilerplate that used to live
as five separate nested closures inside ``__main__._run``.

Every payload is a typed :mod:`schemas_push` model — no more raw
``dict`` literals flowing across the wire.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, MutableSequence
from typing import Any

from igni_core.backend.schemas_push import (
    BackgroundProcessDonePayload,
    FileEditedPayload,
    LoginResultPayload,
    LoginStatusPayload,
    ProcessExitedPayload,
    ProcessLinePayload,
    ProcessStartedPayload,
    SchedulerCompletedPayload,
    SchedulerStartedPayload,
    SessionNamedPayload,
)
from igni_core.backend.schemas_sessions import TransportLike
from igni_core.backend.schemas_workflow_events import WorkflowEventRecord
from igni_core.backend.server import BackendServer
from igni_core.backend.workspace_trust_controller import WorkspaceTrustStatus
from igni_core.core.activity import background
from igni_core.core.broadcast_schema import BroadcastEvent
from igni_core.core.tools.edit import (
    FileEditNotifier,
    default_file_edit_notifier,
)
from igni_core.core.tools.process_events import (
    ProcessExitEvent,
    ProcessLineEvent,
    ProcessStartEvent,
)
from igni_core.core.tools.process_supervisor_locator import supervisors
from igni_core.notifications.schemas import NotificationCounts, NotificationsChanged
from igni_core.protocol import messages as msg
from igni_core.protocol.schemas.enums import PushChannel

logger = logging.getLogger(__name__)


class PushNotificationBridge:
    """Wraps a transport + event loop, exposing every push channel
    as a bound method with a typed payload.

    Constructed once during BE boot. The transport it stamps
    through can be replaced per-runtime via :meth:`for_transport`
    (used by :class:`SessionRuntimeFactory` when a pooled runtime's
    ``SessionStampingTransport`` needs to carry its own session id
    on broadcasts).
    """

    def __init__(
        self,
        *,
        transport: TransportLike,
        loop: asyncio.AbstractEventLoop | None,
        queue: MutableSequence[str],
        file_edit_notifier: FileEditNotifier | None = None,
    ) -> None:
        self._transport = transport
        self._loop = loop
        self._queue = queue
        # Fall back to the module-level default so a bridge
        # constructed without an explicit notifier still binds to
        # the same instance ``IgniEditTools`` uses by default. This
        # preserves the shared-instance invariant (both sides
        # observe the same listener) with zero call-site churn.
        self._file_edit_notifier = file_edit_notifier or default_file_edit_notifier
        # How many runtimes the BE is hosting right now. Default
        # ``1`` = "this bridge's session is the only one", which is
        # true for every bridge built before the pool exists and for
        # the whole single-window life of the process. See
        # :meth:`set_live_session_count` for why the count is the
        # thing that gates conversation injection.
        self._live_session_count: Callable[[], int] = lambda: 1

    @classmethod
    def wired(
        cls, *, transport: TransportLike, loop: asyncio.AbstractEventLoop, backend: BackendServer
    ) -> PushNotificationBridge:
        """The boot session's bridge, every push channel bound.

        Its queue is the backend's durable one, so what a previous
        backend left waiting is in it already.
        """
        queue = backend.message_queue
        bridge = cls(transport=transport, loop=loop, queue=queue)
        backend.wire_queue_hook(queue)
        bridge.wire_all(backend)
        return bridge

    # ── Runtime binding ─────────────────────────────────────────

    def set_live_session_count(self, probe: Callable[[], int]) -> None:
        """Wire the "how many sessions is this BE hosting" probe.

        Called by :class:`BackendApp` once the pool exists. Only
        :meth:`_on_process_done` reads it — see the comment there for
        what it protects.
        """
        self._live_session_count = probe

    def for_transport(
        self, transport: TransportLike, *, queue: MutableSequence[str]
    ) -> PushNotificationBridge:
        """Sub-bridge bound to a different transport (typically a
        :class:`SessionStampingTransport` for a pooled runtime). The
        loop + file-edit notifier references are shared with the
        parent bridge.

        ``queue`` is never shared. The queue is a conversation —
        whatever is appended to it is injected as a synthetic user
        message on the owning session's next turn — so a sub-bridge
        that kept the parent's list would speak into the boot session
        no matter which window it belongs to. Callers pass the
        runtime's own queue."""
        sub = PushNotificationBridge(
            transport=transport,
            loop=self._loop,
            queue=queue,
            file_edit_notifier=self._file_edit_notifier,
        )
        sub.set_live_session_count(self._live_session_count)
        return sub

    # ── Login channel ────────────────────────────────────────────

    async def on_login_status(self, text: str) -> None:
        await self._transport.send(msg.push_login_status(LoginStatusPayload(text=text)))

    async def on_login_result(self, success: bool, result: str) -> None:
        await self._transport.send(
            msg.push_login_result(LoginResultPayload(success=success, result=result))
        )

    # ── Scheduler channel ────────────────────────────────────────

    def _on_scheduler_started(self, task_id: str, description: str) -> None:
        payload = SchedulerStartedPayload(task_id=task_id, description=description)
        # A message to the windows: too short to count as busy.
        background.daemon(self._transport.send(msg.push_scheduler_started(payload)), name="push")

    def _on_scheduler_completed(
        self, task_id: str, description: str, succeeded: bool, detail: str = ""
    ) -> None:
        """Publish a scheduled task's outcome.

        ``detail`` carries the task's result text, or the error on failure. The
        runner used to pass its success flag into this ``result`` slot, and
        ``SchedulerCompletedPayload.result`` is typed ``str``, so constructing
        the payload raised ``ValidationError`` on every single completion and
        the push was never sent. The parameter is named for what it is now, so
        the next person to wire a caller cannot quietly repeat it.
        """
        payload = SchedulerCompletedPayload(
            task_id=task_id,
            description=description,
            result=detail if detail else ("completed" if succeeded else "failed"),
        )
        background.daemon(self._transport.send(msg.push_scheduler_completed(payload)), name="push")

    def start_scheduler(self, backend: BackendServer) -> None:
        """Kick off the scheduled-task poller with our push
        callbacks. Replaces the old free-function
        ``_start_scheduler_with_push``."""
        backend.controllers.loop.scheduler.start(
            on_task_started=self._on_scheduler_started,
            on_task_completed=self._on_scheduler_completed,
        )

    # ── Background-process channel ──────────────────────────────

    def bind_to_process_supervisor(self) -> None:
        """Subscribe our per-line / start / exit handlers on the
        default process registry bus. Idempotent-safe: the bus
        uses an append-only subscriber list.

        Post-split (see ``process_supervisor.py`` module docstring)
        the bus lives on
        :class:`~igni_core.core.tools.process_registry.ProcessRegistry`
        rather than on the supervisor — event emission co-locates
        with the state that emits it.

        The bus hands every subscriber the typed event model for the
        event it registered against, so a handler reads
        ``event.pid`` / ``event.line`` directly and no mapping is
        built anywhere on the way.
        """
        bus = supervisors.default().registry.bus
        bus.on("start", self._on_process_started)
        bus.on("line", self._on_process_line)
        bus.on("exit", self._on_process_exited)
        bus.on("exit", self._on_process_done)

    def _on_process_started(self, event: ProcessStartEvent) -> None:
        payload = ProcessStartedPayload(pid=event.pid, cmd=event.cmd, started_at=event.started_at)
        self._schedule_push(PushChannel.PROCESS_STARTED, payload.model_dump())

    def _on_process_line(self, event: ProcessLineEvent) -> None:
        payload = ProcessLinePayload(pid=event.pid, line=event.line)
        self._schedule_push(PushChannel.PROCESS_LINE, payload.model_dump())

    def _on_process_exited(self, event: ProcessExitEvent) -> None:
        payload = ProcessExitedPayload(
            pid=event.pid,
            cmd=event.cmd,
            exit_code=event.exit_code,
            duration_seconds=event.duration_seconds,
        )
        self._schedule_push(PushChannel.PROCESS_EXITED, payload.model_dump())

    def _on_process_done(self, event: ProcessExitEvent) -> None:
        """Long-form background-process completion notice + queue
        injection. The queue-injected text lands as a synthetic
        user message on the next tool result so the agent reacts to
        the completion naturally."""
        status = "succeeded" if event.exit_code == 0 else f"failed (exit {event.exit_code})"
        # Include a one-line hint pointing at ``read_process_output``
        # so the agent knows it can pull more than the 40-line tail.
        hint = f"For more output: read_process_output({event.pid}, tail=N)"
        if event.output_tail:
            msg_text = (
                f"BACKGROUND PROCESS COMPLETED\n"
                f"PID {event.pid}: {event.cmd}\n"
                f"Status: {status}  ·  Duration: {event.duration_seconds:.1f}s\n\n"
                f"Last output (tail):\n{event.output_tail}\n\n{hint}"
            )
        else:
            msg_text = (
                f"BACKGROUND PROCESS COMPLETED\n"
                f"PID {event.pid}: {event.cmd}\n"
                f"Status: {status}  ·  Duration: {event.duration_seconds:.1f}s\n\n{hint}"
            )
        # The FE push below is addressed to a pid, so it is always
        # safe. The queue append is not: it becomes a synthetic user
        # message in THIS bridge's conversation, and the process bus
        # is process-wide (one ``supervisors.default()`` for every
        # session in the BE — see ``bind_to_process_supervisor``).
        # Only the boot bridge subscribes, so with a pool live, a
        # build finishing in one window was narrated into the boot
        # session's conversation — a different window, potentially a
        # different repository (a pooled runtime can be attached to
        # its own ``project_dir``). There is no owner on the event to
        # check against, so the only sound rule left is: inject only
        # when there is exactly one conversation it could belong to.
        # The watcher panel still gets the completion either way.
        if self._live_session_count() <= 1:
            self._queue.append(msg_text)
        else:
            logger.debug(
                "pid %s completed while %d sessions are live; "
                "skipping conversation injection (no owner on the event)",
                event.pid,
                self._live_session_count(),
            )
        payload = BackgroundProcessDonePayload(
            pid=event.pid,
            cmd=event.cmd,
            exit_code=event.exit_code,
            duration_seconds=event.duration_seconds,
        )
        self._schedule_push(PushChannel.BACKGROUND_PROCESS_DONE, payload.model_dump())

    # ── File-edit channel ───────────────────────────────────────

    def bind_to_file_edit_listener(self) -> None:
        """Register our path-forwarding callback with the shared
        :class:`FileEditNotifier`. The notifier reference was
        captured at construction time — see ``__init__``."""
        self._file_edit_notifier.set_listener(self._on_file_edited)

    def _on_file_edited(self, abs_path: str) -> None:
        payload = FileEditedPayload(path=abs_path)
        self._schedule_push(PushChannel.FILE_EDITED, payload.model_dump())

    # ── Broadcast bus ───────────────────────────────────────────

    def bind_to_broadcast_bus(self, backend: BackendServer) -> None:
        """Wire the backend's broadcast bus to fan every broadcast
        out as a :class:`PushNotification` stamped for this bridge's
        transport. Pooled runtimes bind a bridge whose transport is
        their :class:`SessionStampingTransport` so ``session_id`` is
        auto-stamped on every push.

        Uses :meth:`BackendServer.register_broadcast` — no reach
        into ``backend._session.broadcast_bus`` — so the seam is
        typed at BackendServer, not scattered across coordinators.
        """
        backend.register_broadcast(self._on_broadcast)

    def _on_broadcast(self, event: BroadcastEvent) -> None:
        """Sync-callable broadcast handler (session.broadcast is
        sync and may be called from a tool-call context that isn't
        itself awaiting). We hop onto the loop via
        ``call_soon_threadsafe``.

        The event is serialised here, at the transport boundary."""
        self._schedule_push(event.channel, event.to_payload())

    # ── Orchestrate progress ────────────────────────────────────

    def on_orchestrate_progress(self, event: dict[str, Any]) -> None:
        """Forward one orchestrate sub-agent event (the
        :data:`~igni_core.core.tools.orchestrate_events.OnProgress`
        contract: an ``_EventBase`` model already dumped at the emit
        site) onto ``orchestrate_event``. Kept sync — the tool fires it
        from inside a run without awaiting."""
        self._schedule_push(PushChannel.ORCHESTRATE_EVENT, event)

    # ── Session naming ──────────────────────────────────────────

    async def on_session_named(self, session_id: str, name: str) -> None:
        payload = SessionNamedPayload(session_id=session_id, name=name)
        await self._transport.send(msg.push_session_named(payload))

    # ── Composite wiring ────────────────────────────────────────

    def wire_all(self, backend: BackendServer) -> None:
        """One call = every push channel bound to its source.
        Called by :meth:`wired` after backend + session are
        up. Individual ``bind_*`` methods stay public so tests can
        wire selectively."""
        self.bind_to_process_supervisor()
        self.bind_to_file_edit_listener()
        backend.wire_orchestrate_progress(self.on_orchestrate_progress)
        self.bind_to_broadcast_bus(backend)

    def notifications_changed(self, counts: NotificationCounts) -> None:
        """The floor's unread counts, after either path moved them."""
        self._schedule_push(
            PushChannel.NOTIFICATIONS_CHANGED,
            NotificationsChanged(counts=counts).model_dump(),
        )

    def workspace_trust(self, status: WorkspaceTrustStatus) -> None:
        """The folder's workspace-trust status, after an answer (or when a
        window must ask): every window showing the trust dialog for that
        folder closes it, or opens it when ``state`` is ``ask``."""
        self._schedule_push(PushChannel.WORKSPACE_TRUST_REQUIRED, status.model_dump())

    def workflow_event(self, record: WorkflowEventRecord) -> None:
        """One event of a running workflow, for the window folding it into
        that run's chat item."""
        self._schedule_push(PushChannel.WORKFLOW_EVENT, record.model_dump())

    # ── Private helpers ─────────────────────────────────────────

    def _schedule_push(self, channel: str, payload: dict[str, Any]) -> None:
        """Hop onto the running loop and enqueue a
        :class:`PushNotification`. Safe to call from any thread —
        the reader thread + the file-edit listener thread + the
        orchestrate progress thread all end up here."""

        def _send() -> None:
            # A message to the windows: too short to count as busy.
            background.daemon(
                self._transport.send(msg.PushNotification(channel=channel, payload=payload)),
                name=f"push-{channel}",
            )

        # Event loop closed during shutdown — drop the push instead
        # of crashing the callback thread. ``loop`` is optional so
        # test harnesses that only exercise sync RPC handlers can
        # construct the bridge without a running loop.
        if self._loop is None:
            return
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(_send)
