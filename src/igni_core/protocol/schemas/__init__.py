"""Protocol wire schemas — split out of :mod:`igni_core.protocol.messages`.

This package holds every Pydantic model in the BE↔FE wire contract,
grouped by concern:

* :mod:`.envelope` — base :class:`Message` + :class:`RunHeader` +
  :class:`RunScopedMessage` mixin.
* :mod:`.enums` — every wire-contract StrEnum (retypes of previously
  free-string fields).
* :mod:`.be_events` — BE → FE run / tool / task / status events.
* :mod:`.mirroring` — multi-client session broadcasts (Welcome,
  Typing, UserMessageReceived, RequirementResolved, RequirementDetails).
* :mod:`.fe_actions` — FE → BE actions (UserMessage, HITLResponse,
  slash Command, Cancel, session/model switch, …).
* :mod:`.rpc` — process-split RPC (RPCRequest, RPCResponse with
  ``ok`` / ``fail`` factories).
* :mod:`.push` — BE → FE push notifications with typed channel enum
  and per-channel factory helpers; :mod:`.push_payloads` — the payload
  model each of those factories takes.
* :mod:`.internal` — intermediate value objects the BE serializer
  builds and then unpacks into wire messages (:class:`ToolResultData`);
  NOT itself on the wire.
* :mod:`.wire` — the wire contract itself: the list of message
  classes that can arrive, and the ``type``-discriminated union they
  form.

The public export surface lives on this package's ``__all__`` so
:mod:`igni_core.protocol.messages` can re-export the entire wire
contract via ``from igni_core.protocol.schemas import *``.

The contract the transports accept is stated separately, in
:mod:`.wire` — :class:`igni_core.protocol.registry.MessageRegistry`
reads that list instead of scanning an import namespace, so a message
that reaches a schema module but not that list is a visible omission
rather than a silent one.
"""

from igni_core.protocol.schemas.be_events import (
    CommandResult,
    ContentDelta,
    Error,
    HITLRequest,
    Info,
    ModelCompleted,
    ReasoningStarted,
    RunCompleted,
    RunError,
    RunPaused,
    RunStarted,
    SchedulerEvent,
    SessionCleared,
    SessionListEntry,
    SessionListResult,
    StatusUpdate,
    StreamingDone,
    TaskCreated,
    TaskIteration,
    TaskSnapshot,
    TaskStateUpdated,
    TaskUpdated,
    ToolCompleted,
    ToolError,
    ToolStarted,
)
from igni_core.protocol.schemas.enums import (
    CommandAction,
    CommandResultKind,
    HITLAction,
    HITLChoice,
    OrchestrationTaskStatus,
    PermissionModeName,
    PushChannel,
    SchedulerEventType,
)
from igni_core.protocol.schemas.envelope import Message, RunHeader, RunScopedMessage
from igni_core.protocol.schemas.fe_actions import (
    Cancel,
    CancelLogin,
    Command,
    HITLDecision,
    HITLResponse,
    HITLResponseBatch,
    MCPToggle,
    ModelSwitch,
    QueueMessage,
    SessionList,
    SessionSwitch,
    Shutdown,
    StreamEnd,
    UserMessage,
)
from igni_core.protocol.schemas.internal import ToolResultData
from igni_core.protocol.schemas.mirroring import (
    RequirementDetails,
    RequirementResolved,
    Typing,
    UserMessageReceived,
    Welcome,
)
from igni_core.protocol.schemas.push import (
    PushNotification,
    PushPayload,
    push_background_process_done,
    push_file_edited,
    push_login_result,
    push_login_status,
    push_on,
    push_process_exited,
    push_process_line,
    push_process_started,
    push_scheduler_completed,
    push_scheduler_started,
    push_session_named,
)
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
from igni_core.protocol.schemas.rpc import RpcErrorCode, RPCRequest, RPCResponse

__all__ = [
    # Envelope + shared value objects
    "Message",
    "RunHeader",
    "RunScopedMessage",
    # Enums
    "CommandAction",
    "CommandResultKind",
    "HITLAction",
    "HITLChoice",
    "OrchestrationTaskStatus",
    "PermissionModeName",
    "PushChannel",
    "SchedulerEventType",
    # BE → FE events
    "CommandResult",
    "ContentDelta",
    "Error",
    "HITLRequest",
    "Info",
    "ModelCompleted",
    "ReasoningStarted",
    "RunCompleted",
    "RunError",
    "RunPaused",
    "RunStarted",
    "SchedulerEvent",
    "SessionCleared",
    "SessionListEntry",
    "SessionListResult",
    "StatusUpdate",
    "StreamingDone",
    "TaskCreated",
    "TaskIteration",
    "TaskSnapshot",
    "TaskStateUpdated",
    "TaskUpdated",
    "ToolCompleted",
    "ToolError",
    "ToolStarted",
    # Mirroring
    "RequirementResolved",
    "RequirementDetails",
    "Typing",
    "UserMessageReceived",
    "Welcome",
    # FE → BE actions
    "Cancel",
    "CancelLogin",
    "Command",
    "HITLDecision",
    "HITLResponse",
    "HITLResponseBatch",
    "MCPToggle",
    "ModelSwitch",
    "QueueMessage",
    "SessionList",
    "SessionSwitch",
    "Shutdown",
    "StreamEnd",
    "UserMessage",
    # RPC
    "RpcErrorCode",
    "RPCRequest",
    "RPCResponse",
    # Push
    "PushNotification",
    "PushPayload",
    "BackgroundProcessDonePayload",
    "FileEditedPayload",
    "LoginResultPayload",
    "LoginStatusPayload",
    "ProcessExitedPayload",
    "ProcessLinePayload",
    "ProcessStartedPayload",
    "SchedulerCompletedPayload",
    "SchedulerStartedPayload",
    "SessionNamedPayload",
    "push_background_process_done",
    "push_file_edited",
    "push_login_result",
    "push_login_status",
    "push_on",
    "push_process_exited",
    "push_process_line",
    "push_process_started",
    "push_scheduler_completed",
    "push_scheduler_started",
    "push_session_named",
    # Internal (not on wire)
    "ToolResultData",
]
