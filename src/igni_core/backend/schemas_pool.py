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
from igni_core.extensions.host.attachments import ChatAttachments
from igni_core.extensions.service_events import DeliveryReceipt, NotHandled, ServiceEvent
from igni_core.protocol.schemas.rpc import RpcErrorCode


class AttachSessionParams(RpcArgs):
    """Which session to attach a connection to, and in which folder."""

    session_id: str = Field(
        default="",
        description="The session to attach to; empty for the default one, or a new one "
        "when `project_dir` is given.",
    )
    project_dir: str = Field(
        default="", description="The folder the session works in; empty for igni's own."
    )

    @field_validator("session_id", "project_dir", mode="before")
    @classmethod
    def _blank_when_absent(cls, value: Any) -> str:
        return str(value or "")


class AttachRefused(BaseModel):
    """Why a window's attach was refused: not a directory, or another project's session."""

    reason: str


class VmSignInParams(RpcArgs):
    """A person's igni server session, for a cloud VM to sign in with."""

    session_token: str = Field(default="", description="The person's igni server session token.")
    organisation: str | None = Field(
        default=None,
        description="The organisation the VM signs in for; `null` for the person's own.",
    )

    @field_validator("session_token", mode="before")
    @classmethod
    def _token_or_blank(cls, value: Any) -> str:
        return value if isinstance(value, str) else ""

    @field_validator("organisation", mode="before")
    @classmethod
    def _organisation_or_none(cls, value: Any) -> str | None:
        return value if isinstance(value, str) else None


class ServiceEventsParams(RpcArgs):
    """A batch of events from extension services, to deliver to sessions."""

    events: list[ServiceEvent] = Field(default_factory=list, description="The events to deliver.")


class ServiceEventsAck(BaseModel):
    """What became of each event in a batch, and which events the sender
    may now forget. Each list holds event ids."""

    delivered: list[str] = Field(description="Events delivered now.")
    duplicate: list[str] = Field(description="Events delivered before; nothing else happened.")
    failed: list[str] = Field(
        description="Events not delivered because the session could not take them. They "
        "are not acknowledged, so send them again."
    )
    not_handled: list[NotHandled] = Field(
        description="Events that no handler took, and why. They are acknowledged, since "
        "sending them again would not change that."
    )
    acknowledge: list[str] = Field(
        description="Every event the sender may forget: `delivered`, `not_handled` and "
        "`duplicate` together."
    )

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
    """The state of a cloud VM's own credential after it signed in."""

    # The credential keeper answers with its own model; it is read into
    # this one, so what goes on the wire is what is published.
    state: Literal["ready", "needs_sign_in", "needs_reissue", "unreachable"] = Field(
        description="`ready`: the VM holds a credential and a token from it. "
        "`needs_sign_in`: the person must sign in through the gateway again. "
        "`needs_reissue`: an app VM's credential is gone and an organisation admin must "
        "re-issue it. `unreachable`: igni server could not be reached; the last token "
        "holds until it expires."
    )
    message: str = Field(default="", description="What went wrong; empty when nothing did.")


class ApproverTokenMinted(BaseModel):
    """A new token that lets a program answer the agent's approval requests."""

    token: str = Field(
        description="The token. A program that connects with it may answer approval "
        "requests and approve plans. It is valid until igni exits or the tokens are revoked."
    )


class ApproverTokensRevoked(BaseModel):
    """How many approver tokens were revoked."""

    revoked: int = Field(description="The number of tokens revoked.")


class ExtensionPost(BaseModel):
    """A message for a session's chat, labelled as an extension's.

    ``queued`` waits for the session's next turn instead of starting one;
    ``from_service`` is the client id of the extension's service when the
    service sent it (``chat.message_added``'s ``from_service``).
    """

    session_id: str
    extension_id: str
    text: str
    media: ChatAttachments = Field(default_factory=ChatAttachments)
    queued: bool
    from_service: str = ""

    @field_validator("media", mode="before")
    @classmethod
    def _no_media_is_empty(cls, value: Any) -> Any:
        return ChatAttachments() if value is None else value


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
