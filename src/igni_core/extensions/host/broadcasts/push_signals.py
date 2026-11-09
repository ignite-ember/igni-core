"""The push channels igni's windows hear, as signals — one handler per channel.

A channel with no handler is dropped: it is either the model's own
plumbing (``coverage.PUSH_UNREACHABLE``) or not yet a channel at all.
``tests/test_what_igni_sends_its_windows_reaches_extensions_as_signals.py``
holds :meth:`PushSignals.channels` to ``coverage.PUSH``.
"""

from __future__ import annotations

from typing import Any, ClassVar

from igni_core.extensions.host.broadcasts.deferred_reads import DeferredReads
from igni_core.extensions.host.broadcasts.file_edits import FileEditSignals
from igni_core.extensions.host.broadcasts.push_payloads import (
    EventPush,
    NotificationsPush,
    PermissionModePush,
    SessionNamedPush,
    ViewActionPush,
    VisualizationDelta,
)
from igni_core.extensions.host.broadcasts.signal import PushHandler, merged
from igni_core.extensions.host.broadcasts.task_signals import TaskSignals
from igni_core.extensions.host.broadcasts.views import ViewAction, ViewShown
from igni_core.extensions.host.signal_values import Emitter, Signal
from igni_core.extensions.host.values import NotificationCounts
from igni_core.protocol import messages as msg
from igni_core.protocol.schemas.enums import PushChannel

NOTIFICATIONS_CHANGED = "notifications.changed"


class AgentEvent(Signal):
    WIRE: ClassVar[str] = "chat.agent_event"

    session_id: str
    kind: str
    #: The orchestration event as pushed; the host schema publishes it as ``json``.
    payload: dict[str, Any]


class PlanChanged(Signal):
    WIRE: ClassVar[str] = "plan.changed"

    session_id: str
    kind: str
    payload: dict[str, Any]


class RetrySignal(Signal):
    WIRE: ClassVar[str] = "chat.retry"

    session_id: str
    kind: str
    payload: dict[str, Any]


class ProcessesChanged(Signal):
    WIRE: ClassVar[str] = "processes.changed"

    kind: str
    payload: dict[str, Any]


class WorkflowsChanged(Signal):
    WIRE: ClassVar[str] = "workflows.changed"

    what: str
    #: As the workflow named itself.
    name: Any
    payload: dict[str, Any]


class SessionNamed(Signal):
    WIRE: ClassVar[str] = "sessions.named"

    session_id: str
    name: str


class PermissionModeChanged(Signal):
    WIRE: ClassVar[str] = "permissions.mode_changed"

    mode: str
    previous: str


class PushSignals:
    """Translates each push channel through one table."""

    PLAN_KINDS: ClassVar[dict[PushChannel, str]] = {
        PushChannel.PLAN_SUBMITTED: "submitted",
        PushChannel.PLAN_DECIDED: "decided",
    }
    PROCESS_KINDS: ClassVar[dict[PushChannel, str]] = {
        PushChannel.PROCESS_STARTED: "started",
        PushChannel.PROCESS_LINE: "line",
        PushChannel.PROCESS_EXITED: "exited",
        PushChannel.BACKGROUND_PROCESS_DONE: "done",
    }
    RETRY_KINDS: ClassVar[dict[PushChannel, str]] = {
        PushChannel.HTTP_RETRY_ATTEMPT: "attempt",
        PushChannel.HTTP_RETRY_SUCCEEDED: "succeeded",
    }

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
            },
            dict.fromkeys(self.PLAN_KINDS, self._plan),
            dict.fromkeys(self.PROCESS_KINDS, self._process),
            dict.fromkeys(self.RETRY_KINDS, self._retry),
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
        delta = VisualizationDelta.model_validate(m.payload)
        if delta.is_delta():
            # A spec still streaming is nothing yet; a finished one is a view.
            if delta.final:
                self._emitter.send(ViewShown.shown(m.session_id, delta))
            return
        kind = EventPush.model_validate(m.payload).type
        self._emitter.send(AgentEvent(session_id=m.session_id, kind=kind, payload=dict(m.payload)))

    def _session_named(self, m: msg.PushNotification) -> None:
        push = SessionNamedPush.model_validate(m.payload)
        self._emitter.send(SessionNamed(session_id=push.session_id or m.session_id, name=push.name))

    def _permission_mode_changed(self, m: msg.PushNotification) -> None:
        push = PermissionModePush.model_validate(m.payload)
        self._emitter.send(PermissionModeChanged(mode=push.mode, previous=push.previous))

    def _plan(self, m: msg.PushNotification) -> None:
        kind = self.PLAN_KINDS[PushChannel(m.channel)]
        self._emitter.send(PlanChanged(session_id=m.session_id, kind=kind, payload=dict(m.payload)))

    def _process(self, m: msg.PushNotification) -> None:
        kind = self.PROCESS_KINDS[PushChannel(m.channel)]
        self._emitter.send(ProcessesChanged(kind=kind, payload=dict(m.payload)))

    def _retry(self, m: msg.PushNotification) -> None:
        kind = self.RETRY_KINDS[PushChannel(m.channel)]
        self._emitter.send(RetrySignal(session_id=m.session_id, kind=kind, payload=dict(m.payload)))

    def _workflow_event(self, m: msg.PushNotification) -> None:
        push = EventPush.model_validate(m.payload)
        self._emitter.send(
            WorkflowsChanged(what=push.type, name=push.name, payload=dict(m.payload))
        )

    def _notifications_changed(self, m: msg.PushNotification) -> None:
        counts = NotificationsPush.model_validate(m.payload).counts
        self._emitter.value(
            NOTIFICATIONS_CHANGED,
            None,
            content=lambda grants: (
                NotificationCounts.from_counts(counts).visible_to(grants).model_dump()
            ),
        )

    def _view_action(self, m: msg.PushNotification) -> None:
        push = ViewActionPush.model_validate(m.payload)
        self._emitter.send(ViewAction.from_payload(m.session_id, push))


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
