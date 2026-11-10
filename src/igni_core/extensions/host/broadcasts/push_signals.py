"""The push channels igni's windows hear, as signals — one handler per channel.

A channel with no handler is dropped: it is either the model's own
plumbing (``coverage.PUSH_UNREACHABLE``) or not yet a channel at all.
``tests/test_what_igni_sends_its_windows_reaches_extensions_as_signals.py``
holds :meth:`PushSignals.channels` to ``coverage.PUSH``.

Each handler reads the push's payload through the model that produced
it (:mod:`.push_reads`), so what an extension is told is typed: the
signal carries the event's own model, not the raw mapping that came
off the wire.
"""

from __future__ import annotations

from typing import ClassVar

from igni_core.backend.schemas_visualization import VisualizationActionDispatchedEvent
from igni_core.backend.schemas_workflow_events import WorkflowEventRecord
from igni_core.core.session.event_log_schema import VisualizationDeltaPayload
from igni_core.core.session.schemas import PermissionModeChangedEvent, PlanDecidedEvent
from igni_core.core.tools.orchestrate_events import ORCHESTRATE_EVENT_ADAPTER, OrchestrateEvent
from igni_core.core.tools.plan.schemas import PlanModeEnteredEvent, PlanSubmittedEvent
from igni_core.core.tools.web_schemas import HttpRetryAttemptEvent, HttpRetrySucceededEvent
from igni_core.extensions.host.broadcasts.deferred_reads import DeferredReads
from igni_core.extensions.host.broadcasts.file_edits import FileEditSignals
from igni_core.extensions.host.broadcasts.push_reads import (
    read_push,
    read_push_from,
    read_union,
)
from igni_core.extensions.host.broadcasts.signal import PushHandler, merged
from igni_core.extensions.host.broadcasts.task_signals import TaskSignals
from igni_core.extensions.host.broadcasts.views import ViewAction, ViewShown
from igni_core.extensions.host.signal_values import Emitter, Signal
from igni_core.extensions.host.values import NotificationCounts
from igni_core.notifications.schemas import NotificationsChanged
from igni_core.protocol import messages as msg
from igni_core.protocol.schemas.enums import PushChannel
from igni_core.protocol.schemas.push_payloads import (
    BackgroundProcessDonePayload,
    ProcessExitedPayload,
    ProcessLinePayload,
    ProcessStartedPayload,
    SessionNamedPayload,
)

NOTIFICATIONS_CHANGED = "notifications.changed"


class AgentEvent(Signal):
    WIRE: ClassVar[str] = "chat.agent_event"

    session_id: str
    kind: str
    #: The orchestration event, as the model that produced it.
    payload: OrchestrateEvent


class PlanChanged(Signal):
    WIRE: ClassVar[str] = "plan.changed"

    session_id: str
    kind: str
    #: The plan's event, as the model that produced it.
    payload: PlanSubmittedEvent | PlanDecidedEvent


class RetrySignal(Signal):
    WIRE: ClassVar[str] = "chat.retry"

    session_id: str
    kind: str
    #: The retry's event, as the model that produced it.
    payload: HttpRetryAttemptEvent | HttpRetrySucceededEvent


class ProcessesChanged(Signal):
    WIRE: ClassVar[str] = "processes.changed"

    kind: str
    #: The process's event, as the model that produced it.
    payload: (
        ProcessStartedPayload
        | ProcessLinePayload
        | ProcessExitedPayload
        | BackgroundProcessDonePayload
    )


class WorkflowsChanged(Signal):
    WIRE: ClassVar[str] = "workflows.changed"

    what: str
    #: As the workflow named itself.
    name: str
    #: The workflow's record, as the model that produced it.
    payload: WorkflowEventRecord


class SessionNamed(Signal):
    WIRE: ClassVar[str] = "sessions.named"

    session_id: str
    name: str


class PermissionModeChanged(Signal):
    WIRE: ClassVar[str] = "permissions.mode_changed"

    mode: str
    #: The mode before this one, when the push named it.
    previous: str | None


class PushSignals:
    """Translates each push channel through one table."""

    def __init__(
        self,
        emitter: Emitter,
        files: FileEditSignals,
        tasks: TaskSignals,
        reads: DeferredReads,
    ) -> None:
        self._emitter = emitter
        # Keyed by channel name: a ``PushChannel`` is a ``str`` and looks up
        # as one, and a channel igni pushes that is not listed finds nothing.
        self._table: dict[str, PushHandler] = merged(
            {
                PushChannel.ORCHESTRATE_EVENT: self._orchestrate_event,
                PushChannel.LOGIN_STATUS: lambda m: reads.account(m.session_id),
                PushChannel.LOGIN_RESULT: lambda m: reads.account(m.session_id),
                PushChannel.OUTPUT_STYLE_CHANGED: lambda m: reads.display(m.session_id),
                PushChannel.SESSION_NAMED: self._session_named,
                PushChannel.PERMISSION_MODE_CHANGED: self._permission_mode_changed,
                PushChannel.NOTIFICATIONS_CHANGED: self._notifications_changed,
                PushChannel.WORKFLOW_EVENT: self._workflow_event,
                PushChannel.VISUALIZATION_ACTION_DISPATCHED: self._view_action,
                PushChannel.PLAN_SUBMITTED: self._plan_submitted,
                PushChannel.PLAN_DECIDED: self._plan_decided,
                PushChannel.PROCESS_STARTED: self._process_started,
                PushChannel.PROCESS_LINE: self._process_line,
                PushChannel.PROCESS_EXITED: self._process_exited,
                PushChannel.BACKGROUND_PROCESS_DONE: self._process_done,
                PushChannel.HTTP_RETRY_ATTEMPT: self._retry_attempt,
                PushChannel.HTTP_RETRY_SUCCEEDED: self._retry_succeeded,
            },
            files.push_handlers(),
            tasks.push_handlers(),
        )

    def channels(self) -> frozenset[PushChannel]:
        """Every channel translated, for the coverage test."""
        return frozenset(PushChannel(channel) for channel in self._table)

    def push(self, m: msg.PushNotification) -> None:
        handler = self._table.get(m.channel)
        if handler is not None:
            handler(m)

    def _orchestrate_event(self, m: msg.PushNotification) -> None:
        """A sub-agent moved on — or a view was shown."""
        if m.payload.get("type") == "visualization_delta":
            delta = read_push(VisualizationDeltaPayload, m.payload, channel=m.channel)
            if delta is None:
                return
            # A spec still streaming is nothing yet; a finished one is a view.
            if delta.final:
                self._emitter.send(ViewShown.shown(m.session_id, delta))
            return
        event = read_union(ORCHESTRATE_EVENT_ADAPTER, m.payload, channel=m.channel)
        if event is None:
            return
        self._emitter.send(AgentEvent(session_id=m.session_id, kind=event.type, payload=event))

    def _session_named(self, m: msg.PushNotification) -> None:
        payload = read_push(SessionNamedPayload, m.payload, channel=m.channel)
        if payload is None:
            return
        self._emitter.send(
            SessionNamed(session_id=payload.session_id or m.session_id, name=payload.name)
        )

    def _permission_mode_changed(self, m: msg.PushNotification) -> None:
        """The mode changed — or plan mode was entered, which names no mode before."""
        event = read_push_from(
            (PermissionModeChangedEvent, PlanModeEnteredEvent),
            m.payload,
            channel=m.channel,
        )
        if event is None:
            return
        if isinstance(event, PermissionModeChangedEvent):
            self._emitter.send(PermissionModeChanged(mode=event.mode, previous=event.previous))
            return
        self._emitter.send(PermissionModeChanged(mode=event.mode, previous=None))

    def _plan_submitted(self, m: msg.PushNotification) -> None:
        payload = read_push(PlanSubmittedEvent, m.payload, channel=m.channel)
        if payload is None:
            return
        self._emitter.send(PlanChanged(session_id=m.session_id, kind="submitted", payload=payload))

    def _plan_decided(self, m: msg.PushNotification) -> None:
        payload = read_push(PlanDecidedEvent, m.payload, channel=m.channel)
        if payload is None:
            return
        self._emitter.send(PlanChanged(session_id=m.session_id, kind="decided", payload=payload))

    def _process_started(self, m: msg.PushNotification) -> None:
        payload = read_push(ProcessStartedPayload, m.payload, channel=m.channel)
        if payload is None:
            return
        self._emitter.send(ProcessesChanged(kind="started", payload=payload))

    def _process_line(self, m: msg.PushNotification) -> None:
        payload = read_push(ProcessLinePayload, m.payload, channel=m.channel)
        if payload is None:
            return
        self._emitter.send(ProcessesChanged(kind="line", payload=payload))

    def _process_exited(self, m: msg.PushNotification) -> None:
        payload = read_push(ProcessExitedPayload, m.payload, channel=m.channel)
        if payload is None:
            return
        self._emitter.send(ProcessesChanged(kind="exited", payload=payload))

    def _process_done(self, m: msg.PushNotification) -> None:
        payload = read_push(BackgroundProcessDonePayload, m.payload, channel=m.channel)
        if payload is None:
            return
        self._emitter.send(ProcessesChanged(kind="done", payload=payload))

    def _retry_attempt(self, m: msg.PushNotification) -> None:
        payload = read_push(HttpRetryAttemptEvent, m.payload, channel=m.channel)
        if payload is None:
            return
        self._emitter.send(RetrySignal(session_id=m.session_id, kind="attempt", payload=payload))

    def _retry_succeeded(self, m: msg.PushNotification) -> None:
        payload = read_push(HttpRetrySucceededEvent, m.payload, channel=m.channel)
        if payload is None:
            return
        self._emitter.send(RetrySignal(session_id=m.session_id, kind="succeeded", payload=payload))

    def _workflow_event(self, m: msg.PushNotification) -> None:
        payload = read_push(WorkflowEventRecord, m.payload, channel=m.channel)
        if payload is None:
            return
        self._emitter.send(WorkflowsChanged(what=payload.type, name=payload.name, payload=payload))

    def _notifications_changed(self, m: msg.PushNotification) -> None:
        payload = read_push(NotificationsChanged, m.payload, channel=m.channel)
        if payload is None:
            return
        counts = payload.counts
        self._emitter.value(
            NOTIFICATIONS_CHANGED,
            None,
            content=lambda grants: (
                NotificationCounts.from_counts(counts.model_dump()).visible_to(grants).model_dump()
            ),
        )

    def _view_action(self, m: msg.PushNotification) -> None:
        payload = read_push(VisualizationActionDispatchedEvent, m.payload, channel=m.channel)
        if payload is None:
            return
        self._emitter.send(ViewAction.dispatched(m.session_id, payload))


__all__ = [
    "AgentEvent",
    "PermissionModeChanged",
    "PlanChanged",
    "ProcessesChanged",
    "PushSignals",
    "RetrySignal",
    "SessionNamed",
    "WorkflowsChanged",
]
