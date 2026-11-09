"""The messages of the host model's channels.

Private to the SDKs: an extension author writes ``ext.host.chat.post(…)``
and never one of these. ``docs/HOST_MODEL_PROTOCOL.md`` describes the
protocol for whoever maintains an SDK.

One request shape serves both channels — the process's loopback
WebSocket and the page's bridge (forwarded as the ``host_model`` RPC) —
so the host answers both through
:meth:`~igni_core.extensions.host.model.HostModel.handle` — one
request, and the :class:`~igni_core.extensions.host.caller.Caller` it
comes from.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_serializer
from pydantic_core import to_jsonable_python

from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.principal import Principal

#: Bumped only for a change to these messages that an SDK cannot
#: ignore. The *model's* version is the schema's, sent in ``hello``.
PROTOCOL_VERSION = 1


class Op(StrEnum):
    GET = "get"
    SET = "set"
    CALL = "call"
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    #: The process's answer to a ``hook`` message. Not a page's to send.
    ANSWER = "answer"


class Request(BaseModel):
    """SDK → host."""

    model_config = ConfigDict(extra="forbid")

    #: Echoed on the reply. Chosen by the SDK; never interpreted.
    id: int | str
    op: Op
    #: ``chat.processing`` for get/set, ``chat.post`` for call. Empty
    #: for subscribe.
    path: str = ""
    #: A window id, for a per-window member addressed to a window other
    #: than the active one (``host.windows.ui(id)``).
    window: str | None = None
    #: A session id, for a per-session member addressed to a session other
    #: than the one on screen (``host.sessions.chat(id)``).
    session: str | None = None
    #: A method's parameters, by name.
    params: dict[str, Any] = Field(default_factory=dict)
    #: The value of a ``set``.
    value: Any = None
    #: Signal wire names, for subscribe and unsubscribe.
    signals: list[str] = Field(default_factory=list)


class ErrorBody(BaseModel):
    code: ErrorCode
    message: str


class Reply(BaseModel):
    """Host → SDK: the answer to one request."""

    op: Literal["reply"] = "reply"
    id: int | str
    ok: bool
    result: Any = None
    error: ErrorBody | None = None

    @classmethod
    def answered(cls, id: int | str, result: Any = None) -> Reply:
        """The request was answered, with ``result``."""
        return cls(id=id, ok=True, result=result)

    @classmethod
    def refused(cls, id: int | str, error: ErrorBody) -> Reply:
        """The request was refused, or failed, for the reason ``error`` gives."""
        return cls(id=id, ok=False, error=error)


class SignalMessage(BaseModel):
    """Host → SDK: a signal fired."""

    op: Literal["signal"] = "signal"
    #: The signal's wire name (``chat.run_completed``, ``session.switched``).
    signal: str
    value: Any = None


class HookFrameValue(BaseModel):
    """The value a hook frame carries: the event's value, and the host's own field.

    Flat on the wire — the event's own fields, then
    ``tool_manifest_name`` — as the protocol says, so an SDK sees one
    object. The value itself is the event's own model: a deciding
    event's value, or a told event's payload.
    """

    #: The event's value, as it is handed over.
    body: BaseModel
    #: The name the manifest gives the tool, when the value carries one.
    tool_manifest_name: str | None = None
    #: Whether the fields that are not set are left out: a told event says
    #: what its payload has; a value the event decided on says all of it.
    omit_none: bool = False

    #: The fields the host adds beside the event's own: told to the
    #: extension, and not the event's to change.
    ADDED: ClassVar[frozenset[str]] = frozenset({"tool_manifest_name"})

    @model_serializer(mode="plain")
    def _wire(self) -> dict[str, Any]:
        """The flat object the frame carries."""
        wire = self.body.model_dump(mode="json", exclude_none=self.omit_none)
        if self.tool_manifest_name is not None:
            wire["tool_manifest_name"] = self.tool_manifest_name
        return to_jsonable_python(wire, fallback=str)


class HookMessage(BaseModel):
    """Host → SDK: one of igni's hook events (``docs/HOOKS.md``, *Extensions*).

    With an ``id``, the event decides something and the host waits for
    the process's ``answer``; without one, it is told and nothing waits.
    """

    op: Literal["hook"] = "hook"
    #: Echoed on the answer; ``None`` for an event that is only told.
    id: str | None = None
    #: The hook event: ``PreToolUse``, ``PlanSubmit``, ``SessionStart``.
    event: str
    value: HookFrameValue | None = None
    #: Whom the event is for (``docs/EXTENSION_ROUTING_AND_CONCURRENCY.md``
    #: §6): ``{kind, id, org, group, session}``, the same shape a tool
    #: call carries in ``_meta["igni/principal"]``.
    principal: Principal | None = None


class Outcome(StrEnum):
    ALLOW = "allow"
    REJECT = "reject"
    MODIFY = "modify"
    #: The handler could not decide — it raised. The host treats it as
    #: a silence: the event's fail mode decides.
    FAILED = "failed"


class HookAnswer(BaseModel):
    """SDK → host, as the ``params`` of an ``answer`` request."""

    model_config = ConfigDict(extra="forbid")

    #: The ``id`` of the ``hook`` message it answers.
    hook: str
    outcome: Outcome
    #: Why, for a reject: shown to the agent or the person. What went
    #: wrong, for ``failed``.
    reason: str = ""
    #: The changed value, for a modify: the whole value, as it was handed
    #: over, with only the fields the event lets it change changed.
    #:
    #: Any JSON, not only an object: the wire carries what the
    #: extension sent, and what an answer *means* is read elsewhere
    #: (:mod:`igni_core.extensions.host.answers`). A change that is not
    #: the event's shape is the extension's bug — it is refused as one,
    #: and not counted as a silence.
    value: JsonValue = None


class Hello(BaseModel):
    """Host → SDK, first, on the process's socket."""

    op: Literal["hello"] = "hello"
    protocol: int = PROTOCOL_VERSION
    schema_version: str
    extension_id: str
    #: The permissions this extension holds, so an SDK can say early
    #: that a member will be refused.
    granted: list[str]


__all__ = [
    "PROTOCOL_VERSION",
    "ErrorBody",
    "Hello",
    "HookAnswer",
    "HookFrameValue",
    "HookMessage",
    "Op",
    "Outcome",
    "Reply",
    "Request",
    "SignalMessage",
]
