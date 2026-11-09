"""Typed payloads of the session pool's own calls and seams.

The pool-level RPCs (``backend/pool_rpcs/``) read their arguments into
these models and answer with them; :class:`PoolAnswer` is what every
one of them returns, success or the reason it was not done, and
:class:`~igni_core.backend.pool_replier.PoolReplier` is the one place
that turns it into an ``RPCResponse``.

The argument models are lenient about values on purpose: a window or the
platform that leaves a value out, or sends ``null``, gets the same answer
it got when these were read off a dict. A key they do not name is refused.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, SerializeAsAny, field_validator

from igni_core.backend.rpc_binding import RpcArgs
from igni_core.extensions.service_events import DeliveryReceipt, NotHandled, ServiceEvent
from igni_core.protocol.schemas.rpc import RpcErrorCode


class AttachSessionParams(RpcArgs):
    """``attach_session``: which session, and in which directory; empty for neither."""

    #: The session to bind; empty for the default one, or a new one when
    #: ``project_dir`` is given.
    session_id: str = ""
    #: The folder the session works in; empty for the backend's own.
    project_dir: str = ""

    @field_validator("session_id", "project_dir", mode="before")
    @classmethod
    def _blank_when_absent(cls, value: Any) -> str:
        return str(value or "")


class AttachRefused(BaseModel):
    """Why a window's attach was refused: not a directory, or another project's session."""

    reason: str


class VmSignInParams(RpcArgs):
    """``vm_sign_in``: the person's session, and the organisation it signs in for."""

    #: The person's igni server session, as the gateway holds it.
    session_token: str = ""
    #: The organisation the VM signs in for; ``None`` for the person's own.
    organisation: str | None = None

    @field_validator("session_token", mode="before")
    @classmethod
    def _token_or_blank(cls, value: Any) -> str:
        return value if isinstance(value, str) else ""

    @field_validator("organisation", mode="before")
    @classmethod
    def _organisation_or_none(cls, value: Any) -> str | None:
        return value if isinstance(value, str) else None


class ServiceEventsParams(RpcArgs):
    """``deliver_service_events``: the platform's batch."""

    events: list[ServiceEvent] = Field(default_factory=list)


class ServiceEventsAck(BaseModel):
    """What became of a batch, and which events the platform may forget."""

    delivered: list[str]
    duplicate: list[str]
    failed: list[str]
    not_handled: list[NotHandled]
    acknowledge: list[str]

    @classmethod
    def of(cls, receipt: DeliveryReceipt) -> ServiceEventsAck:
        return cls(
            delivered=receipt.delivered,
            duplicate=receipt.duplicate,
            failed=receipt.failed,
            not_handled=receipt.not_handled,
            acknowledge=receipt.acknowledge,
        )


class VmCredentialState(BaseModel):
    """``vm_sign_in``'s answer: the state of the VM's own credential after the sign-in.

    The layer's credential keeper answers with its own model; it is read
    into this one, so what goes on the wire is what is published.
    """

    #: ``ready``: the VM holds a credential and a token from it.
    #: ``needs_sign_in``: the person must sign in through the gateway again.
    #: ``needs_reissue``: an app VM's credential is gone; an org admin re-issues it.
    #: ``unreachable``: igni server could not be reached; the last token holds
    #: until it expires.
    state: Literal["ready", "needs_sign_in", "needs_reissue", "unreachable"]
    #: What went wrong, when something did.
    message: str = ""


class ApproverTokenMinted(BaseModel):
    """``mint_approver_token``'s answer: the token, for the window that asked."""

    token: str


class ApproverTokensRevoked(BaseModel):
    """``revoke_approver_tokens``'s answer: how many were forgotten."""

    revoked: int


class ExtensionPost(BaseModel):
    """A message for a session's chat, labelled as an extension's.

    ``queued`` waits for the session's next turn instead of starting one;
    ``from_service`` is the client id of the extension's service when the
    service sent it (``chat.message_added``'s ``from_service``).
    """

    session_id: str
    extension_id: str
    text: str
    media: dict[str, str] = Field(default_factory=dict)
    queued: bool
    from_service: str = ""

    @field_validator("media", mode="before")
    @classmethod
    def _no_media_is_empty(cls, value: Any) -> Any:
        return {} if value is None else value


class ProjectDirs(BaseModel):
    """The directories the backend was started in: its project, and any extra roots."""

    project_dir: Path
    additional_dirs: list[Path] | None = None


class PoolAnswer(BaseModel):
    """A pool-level call's answer: a result, or the reason there is none.

    ``dump_mode`` is how the result goes on the wire. Each call keeps the
    mode it always had: a host-model reply and the platform's answers are
    dumped as JSON, the rest as Python values.
    """

    result: SerializeAsAny[BaseModel] | None = None
    error: str = ""
    #: Why there is no result, when there is none.
    code: RpcErrorCode | None = None
    dump_mode: Literal["python", "json"] = "python"

    @classmethod
    def ok(
        cls, result: BaseModel, *, dump_mode: Literal["python", "json"] = "python"
    ) -> PoolAnswer:
        return cls(result=result, dump_mode=dump_mode)

    @classmethod
    def failed(cls, reason: str, code: RpcErrorCode) -> PoolAnswer:
        return cls(error=reason, code=code)

    def wire_result(self) -> Any:
        """The result as the response carries it."""
        return self.result.model_dump(mode=self.dump_mode) if self.result is not None else None


class Refusal(BaseModel):
    """Why a message is not handled at all, from the connection it came on."""

    reason: str
