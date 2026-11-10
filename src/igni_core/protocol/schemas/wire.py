"""The wire contract — every message class that can arrive on the wire.

One list, in one place. :data:`WIRE_MESSAGE_TYPES` names every message
class the transports accept, and :data:`WireMessage` is that same list
as a single ``type``-discriminated union, so a frame is validated
against the contract rather than against a class somebody looked up by
string.

The list used to be discovered by scanning
:mod:`igni_core.protocol.messages` — a re-export shim — for
:class:`Message` subclasses with a defaulted ``type`` field. That made
the wire surface a property of an import namespace: a message added to
the schemas but not re-exported was dropped from the wire without a
word, and a helper class that happened to be importable looked like a
message. Here the surface is stated, so an omission is visible —
``tests/test_protocol_registry.py`` fails when a message class reaches
the schemas without joining this list.

Adding a message is two steps: define the class in the schema module
that owns its concern, then name it here. The test above is what keeps
the second step from being forgotten.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import reduce
from operator import or_
from typing import Annotated, Any, get_args

from pydantic import Field

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
    SessionListResult,
    StatusUpdate,
    StreamingDone,
    TaskCreated,
    TaskIteration,
    TaskStateUpdated,
    TaskUpdated,
    ToolCompleted,
    ToolError,
    ToolStarted,
)
from igni_core.protocol.schemas.envelope import Message
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
from igni_core.protocol.schemas.mirroring import (
    RequirementDetails,
    RequirementResolved,
    Typing,
    UserMessageReceived,
    Welcome,
)
from igni_core.protocol.schemas.push import PushNotification
from igni_core.protocol.schemas.rpc import RPCRequest, RPCResponse

#: Every message class that can appear on the wire.
WIRE_MESSAGE_TYPES: tuple[type[Message], ...] = (
    # BE → FE events
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
    SessionListResult,
    StatusUpdate,
    StreamingDone,
    TaskCreated,
    TaskIteration,
    TaskStateUpdated,
    TaskUpdated,
    ToolCompleted,
    ToolError,
    ToolStarted,
    # FE → BE actions
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
    # Mirroring
    RequirementDetails,
    RequirementResolved,
    Typing,
    UserMessageReceived,
    Welcome,
    # Push
    PushNotification,
    # RPC
    RPCRequest,
    RPCResponse,
)


def wire_union(message_types: Sequence[type[Message]] = WIRE_MESSAGE_TYPES) -> Any:
    """The ``type``-discriminated union over ``message_types``.

    Pydantic picks the member by the frame's ``type`` field and
    validates the rest of the frame against it, so the wire
    discriminator and the registry's lookup table are the same list —
    they cannot drift apart.
    """
    types = tuple(message_types)
    if not types:
        raise ValueError("a wire union needs at least one message type")
    return Annotated[reduce(or_, types), Field(discriminator="type")]


def wire_type_of(message_cls: type[Message]) -> str:
    """The wire ``type`` string a message class carries.

    Read from the field's ``Literal`` annotation — the value Pydantic
    uses as the union's discriminator — so the lookup key and the
    discriminator can never disagree.
    """
    field = message_cls.model_fields.get("type")
    literals = get_args(field.annotation) if field is not None else ()
    if len(literals) == 1 and isinstance(literals[0], str):
        return str(literals[0])
    raise ValueError(
        f"{message_cls.__name__} needs a single Literal 'type' field to be on the wire"
    )


#: The whole wire contract as one discriminated union.
WireMessage = wire_union()


__all__ = ["WIRE_MESSAGE_TYPES", "WireMessage", "wire_type_of", "wire_union"]
