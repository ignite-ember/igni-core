"""The platform's calls: ``vm_sign_in`` and ``deliver_service_events``.

Who may make them is :class:`~igni_core.backend.message_admission.MessageAdmission`'s
to decide (a layer may take them from its control connection alone); these
only answer them.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from igni_core.backend.pool_rpcs.base import PoolRefusal, PoolRpc, UnavailableRpc
from igni_core.backend.rpc_binding import Correlation, RpcArgsError, rpc
from igni_core.backend.rpc_streams import RUN_EVENTS, TOOL_PUSHES
from igni_core.backend.schemas_pool import (
    ServiceEventsAck,
    ServiceEventsParams,
    VmCredentialState,
    VmSignInParams,
)
from igni_core.extensions.service_events import ServiceEventInbox
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcDuration, RpcMethod, RpcScope
from igni_core.protocol.schemas.enums import PushChannel
from igni_core.protocol.schemas.rpc import RpcErrorCode


class VmSignIn(Protocol):
    """Takes a person's session for the VM's own credential (a layer's credential
    keeper); answers its state, as :class:`VmCredentialState` reads it."""

    async def __call__(self, session_token: str, organisation: str | None, /) -> BaseModel: ...


class VmSignInRpc(PoolRpc):
    """``{session_token, organisation?}`` → the credential's state."""

    JSON_DUMPED = frozenset({RpcMethod.VM_SIGN_IN})

    NO_SESSION = "vm_sign_in: a producer's VM takes the person's session_token"

    def __init__(self, sign_in: VmSignIn) -> None:
        self._sign_in = sign_in

    @classmethod
    def unmounted(cls) -> list[UnavailableRpc]:
        return [UnavailableRpc(RpcMethod.VM_SIGN_IN, cls.NO_SESSION)]

    @rpc(
        RpcMethod.VM_SIGN_IN,
        correlate=Correlation(by="channel"),
        duration=RpcDuration.NETWORK,
        args=VmSignInParams,
        scope=RpcScope.IGNI,
        streams=(PushChannel.VM_CREDENTIAL,),
    )
    async def sign_in(self, args: VmSignInParams, request: msg.RPCRequest) -> VmCredentialState:
        """Hand a cloud VM the person's session, for igni to exchange for the VM's own credential.

        Made by the platform's gateway when the person signs in through it.
        ``session_token`` is the person's igni server session; ``organisation``
        the organisation the VM signs in for (``null``: the person's own).

        Returns the credential's ``state`` after the exchange (``ready``,
        ``needs_sign_in``, ``needs_reissue`` or ``unreachable``) and a
        ``message`` saying what went wrong, if anything. A change of state
        is also pushed as ``vm_credential``, now and whenever the credential
        is renewed later.

        Fails when ``session_token`` is empty, or when this igni is not a
        producer's VM (nothing is mounted to take the session).
        """
        if not args.session_token:
            raise PoolRefusal(self.NO_SESSION, RpcErrorCode.INVALID)
        change = await self._sign_in(args.session_token, args.organisation)
        return VmCredentialState.model_validate(change.model_dump(mode="json"))


class ServiceEventRpc(PoolRpc):
    """``{events: [...]}`` → what became of each, and which to acknowledge."""

    JSON_DUMPED = frozenset({RpcMethod.DELIVER_SERVICE_EVENTS})

    NO_INBOX = "this igni takes no events"

    def __init__(self, inbox: ServiceEventInbox) -> None:
        self._inbox = inbox

    @classmethod
    def unmounted(cls) -> list[UnavailableRpc]:
        return [UnavailableRpc(RpcMethod.DELIVER_SERVICE_EVENTS, cls.NO_INBOX)]

    def refused_args(self, exc: RpcArgsError) -> str:
        return f"not events: {exc.__cause__}"

    @rpc(
        RpcMethod.DELIVER_SERVICE_EVENTS,
        correlate=Correlation(by="channel"),
        duration=RpcDuration.IO,
        args=ServiceEventsParams,
        scope=RpcScope.IGNI,
        streams=(
            msg.UserMessageReceived,
            PushChannel.SESSION_NAMED,
            PushChannel.ORCHESTRATE_EVENT,
            *RUN_EVENTS,
        ),
        may_stream=TOOL_PUSHES,
    )
    async def deliver(self, args: ServiceEventsParams, request: msg.RPCRequest) -> ServiceEventsAck:
        """Hand igni the events extensions' services sent it, to start or resume the agent's work.

        Made by the platform (the gateway, in the cloud). ``events`` is the
        batch as the platform hands it out: each names its ``event_id``, the
        ``extension_id`` it is for, and what it carries.

        Returns what became of each event by id: ``delivered``, ``duplicate``
        (delivered before; nothing happened), ``failed`` (the session could
        not take it: leave it unacknowledged so it is handed out again),
        ``not_handled`` (no handler took it, with the reason), and
        ``acknowledge`` — the ids the platform may forget.

        An event goes to its extension's process. Any other item lands in a
        chat: a new thread opens one (named with ``session_named``), a
        message is posted there (``user_message_received``), and, unless it
        is queued or the chat is busy, the agent's run streams as any run
        does. An item's ``action`` can also show progress
        (``orchestrate_event``), rename the chat or cancel its run.

        Fails when the arguments are not a batch of events, or when this
        igni takes no events.
        """
        receipt = await self._inbox.deliver(args.events)
        return ServiceEventsAck.of(receipt)
