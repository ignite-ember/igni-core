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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from igni_core.extensions.host.spec import ErrorCode
from igni_core.protocol.json_value import WireJson

#: Bumped only for a change to these messages that an SDK cannot
#: ignore. The *model's* version is the schema's, sent in ``hello``.
PROTOCOL_VERSION = 1


class Op(StrEnum):
    """What a host-model request does.

    * ``get``: read a property.
    * ``set``: write a property.
    * ``call``: call a method.
    * ``subscribe`` / ``unsubscribe``: start or stop receiving signals.
    * ``answer``: an extension process's answer to a hook event; a page
      may not send it.
    """

    GET = "get"
    SET = "set"
    CALL = "call"
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    ANSWER = "answer"


class Request(BaseModel):
    """A request from an extension to the host model: read, write or call one member, or subscribe to signals."""

    model_config = ConfigDict(extra="forbid")

    id: int | str = Field(
        description="Any id you choose; it is echoed on the reply and never interpreted."
    )
    op: Op = Field(description="What the request does.")
    path: str = Field(
        default="",
        description=(
            "The member it is about, e.g. `chat.processing` for `get`/`set` or `chat.post` "
            "for `call`. Empty for `subscribe` and `unsubscribe`."
        ),
    )
    window: str | None = Field(
        default=None,
        description=(
            "A window id, to address a per-window member of a window other than the active "
            "one (`host.windows.ui(id)`); null for the active window."
        ),
    )
    session: str | None = Field(
        default=None,
        description=(
            "A session id, to address a per-session member of a session other than the one "
            "on screen (`host.sessions.chat(id)`); null for the one on screen."
        ),
    )
    params: dict[str, WireJson] = Field(
        default_factory=dict, description="For `call`: the method's parameters, by name."
    )
    value: WireJson = Field(default=None, description="For `set`: the value to write.")
    signals: list[str] = Field(
        default_factory=list,
        description="For `subscribe` and `unsubscribe`: the signals' names, e.g. `chat.run_completed`.",
    )


class ErrorBody(BaseModel):
    """Why a host-model request was refused or failed."""

    code: ErrorCode = Field(description="Why, as a word code can branch on.")
    message: str = Field(description="What happened, as a sentence for the extension's author.")


class Reply(BaseModel):
    """The host model's answer to one request."""

    op: Literal["reply"] = Field(
        default="reply", description="Always `reply`: what kind of message this is."
    )
    id: int | str = Field(description="The `id` of the request it answers.")
    ok: bool = Field(
        description="True when the request was answered; false when it was refused or failed."
    )
    result: WireJson = Field(
        default=None, description="When `ok`: the value read or the method's result; null for none."
    )
    error: ErrorBody | None = Field(default=None, description="When not `ok`: why; null otherwise.")

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
    value: Any = None
    #: Whom the event is for (``docs/EXTENSION_ROUTING_AND_CONCURRENCY.md``
    #: §6): ``{kind, id, org, group, session}``, the same shape a tool
    #: call carries in ``_meta["igni/principal"]``.
    principal: dict[str, Any] | None = None


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
    value: Any = None


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
    "HookMessage",
    "Op",
    "Outcome",
    "Reply",
    "Request",
    "SignalMessage",
]
