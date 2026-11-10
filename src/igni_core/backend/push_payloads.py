"""The payload model of every push channel.

A :class:`~igni_core.protocol.messages.PushNotification` carries its
payload as a JSON object; which object is the channel's to say. This is
where each channel says it: :data:`PUSH_PAYLOADS` maps every
:class:`~igni_core.protocol.schemas.enums.PushChannel` to the model its
producers dump onto the wire, and ``docs/rpc-contract.json`` publishes
the table (``push_channels``) from here.

A broadcast-bus event (:class:`~igni_core.core.broadcast_schema.BroadcastEvent`)
is its own payload model: its fields, less ``channel``, are the payload
(:meth:`~igni_core.core.broadcast_schema.BroadcastEvent.to_payload`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import Field

from igni_core.backend.schemas_pool import VmCredentialState
from igni_core.backend.schemas_visualization import VisualizationActionDispatchedEvent
from igni_core.backend.schemas_workflow_events import WorkflowEventRecord
from igni_core.backend.workspace_trust_controller import WorkspaceTrustStatus
from igni_core.core.session.event_log_schema import VisualizationDeltaPayload
from igni_core.core.session.schemas import (
    OutputStyleChangedEvent,
    PermissionModeChangedEvent,
    PlanDecidedEvent,
)
from igni_core.core.tools.orchestrate_events import (
    AgentCompletedEvent,
    AgentPausedEvent,
    AgentStartedEvent,
    ContentPreviewEvent,
    RunErrorEvent,
    TaskCreatedEvent,
    TaskUpdatedEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
)
from igni_core.core.tools.plan.schemas import PlanModeEnteredEvent, PlanSubmittedEvent
from igni_core.core.tools.todo import TodosUpdatedEvent
from igni_core.core.tools.web_schemas import HttpRetryAttemptEvent, HttpRetrySucceededEvent
from igni_core.extensions.host.pushes import ExternalOpen, HostSignalPush
from igni_core.extensions.host.windows import WindowChange
from igni_core.notifications.schemas import NotificationsChanged
from igni_core.protocol.schemas.enums import PushChannel
from igni_core.protocol.schemas.push_payloads import (
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

#: One ``orchestrate_event``: a sub-agent's progress, or a view the agent
#: (or an extension) drew, told apart by ``type``.
OrchestrateEvent = Annotated[
    ToolStartedEvent
    | ToolCompletedEvent
    | AgentStartedEvent
    | AgentPausedEvent
    | AgentCompletedEvent
    | RunErrorEvent
    | ContentPreviewEvent
    | TaskCreatedEvent
    | TaskUpdatedEvent
    | VisualizationDeltaPayload,
    Field(discriminator="type"),
]


@dataclass(frozen=True)
class PushPayload:
    """One channel's payload: its model, and how its producers dump it.

    ``mode`` is ``serialization`` for a payload dumped whole, in which
    every field is present; ``validation`` for one dumped with
    ``exclude_none``, in which a field that may be ``null`` may be absent.
    """

    model: Any
    doc: str
    mode: Literal["serialization", "validation"] = "serialization"


PUSH_PAYLOADS: Mapping[PushChannel, PushPayload] = {
    PushChannel.SCHEDULER_STARTED: PushPayload(
        SchedulerStartedPayload, "A scheduled task started running."
    ),
    PushChannel.SCHEDULER_COMPLETED: PushPayload(
        SchedulerCompletedPayload, "A scheduled task finished: its result, or why it failed."
    ),
    PushChannel.ORCHESTRATE_EVENT: PushPayload(
        OrchestrateEvent,
        "A sub-agent's progress (a tool call, its start, pause, end or error, a task) or "
        "a view the agent or an extension drew, told apart by ``type``.",
        mode="validation",
    ),
    PushChannel.LOGIN_STATUS: PushPayload(
        LoginStatusPayload, "What a sign-in started by ``login`` is doing now."
    ),
    PushChannel.LOGIN_RESULT: PushPayload(
        LoginResultPayload, "How a sign-in started by ``login`` ended."
    ),
    PushChannel.SESSION_NAMED: PushPayload(SessionNamedPayload, "A session was given a name."),
    PushChannel.PERMISSION_MODE_CHANGED: PushPayload(
        Annotated[PermissionModeChangedEvent | PlanModeEnteredEvent, Field(discriminator="kind")],
        "The session's permission mode changed: the switch itself (``mode`` and "
        "``previous``), or the plan tool's note that the agent entered plan mode "
        "(``mode``, ``source``, ``reason``).",
    ),
    PushChannel.OUTPUT_STYLE_CHANGED: PushPayload(
        OutputStyleChangedEvent, "The session's output style changed."
    ),
    PushChannel.PLAN_SUBMITTED: PushPayload(
        PlanSubmittedEvent, "The agent submitted a plan for the person to decide on."
    ),
    PushChannel.FILE_EDITED: PushPayload(FileEditedPayload, "An edit tool wrote a file."),
    PushChannel.NOTIFICATIONS_CHANGED: PushPayload(
        NotificationsChanged, "The person's unread notification counts moved."
    ),
    PushChannel.PROCESS_STARTED: PushPayload(
        ProcessStartedPayload, "A background process started."
    ),
    PushChannel.PROCESS_LINE: PushPayload(
        ProcessLinePayload, "A background process printed a line."
    ),
    PushChannel.PROCESS_EXITED: PushPayload(ProcessExitedPayload, "A background process exited."),
    PushChannel.BACKGROUND_PROCESS_DONE: PushPayload(
        BackgroundProcessDonePayload, "A background process finished, as the agent is told."
    ),
    PushChannel.WORKFLOW_EVENT: PushPayload(
        WorkflowEventRecord, "One step of a running workflow, keyed by its run id."
    ),
    PushChannel.TODOS_UPDATED: PushPayload(
        TodosUpdatedEvent, "The agent's to-do list, whole, after it changed."
    ),
    PushChannel.PLAN_DECIDED: PushPayload(
        PlanDecidedEvent, "A submitted plan was approved or dismissed."
    ),
    PushChannel.HTTP_RETRY_ATTEMPT: PushPayload(
        HttpRetryAttemptEvent, "A web fetch is about to be retried."
    ),
    PushChannel.HTTP_RETRY_SUCCEEDED: PushPayload(
        HttpRetrySucceededEvent, "A web fetch succeeded after retrying."
    ),
    PushChannel.VISUALIZATION_ACTION_DISPATCHED: PushPayload(
        VisualizationActionDispatchedEvent, "The person used a control in a view."
    ),
    PushChannel.HOST_SIGNAL: PushPayload(
        HostSignalPush, "A host-model signal for one extension's pages."
    ),
    PushChannel.WINDOW_MODEL_CHANGED: PushPayload(
        WindowChange, "A window's model changed; the window it names applies it."
    ),
    PushChannel.OPEN_EXTERNAL: PushPayload(
        ExternalOpen, "An extension asked a window to open a link in the browser."
    ),
    PushChannel.WORKSPACE_TRUST_REQUIRED: PushPayload(
        WorkspaceTrustStatus,
        "A folder's trust status: windows showing the trust dialog for it close it, "
        "or open it while ``state`` is ``ask``.",
    ),
    PushChannel.VM_CREDENTIAL: PushPayload(
        VmCredentialState, "A cloud VM's own credential changed state."
    ),
}

__all__ = ["PUSH_PAYLOADS", "OrchestrateEvent", "PushPayload"]
