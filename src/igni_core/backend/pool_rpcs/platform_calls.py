"""The platform's calls: ``vm_sign_in`` and ``deliver_service_events``.

Who may make them is :class:`~igni_core.backend.message_admission.MessageAdmission`'s
to decide (a layer may take them from its control connection alone); these
only answer them.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ValidationError

from igni_core.backend.pool_rpcs.base import PoolRpc, UnavailableRpc
from igni_core.backend.schemas_pool import (
    PoolAnswer,
    ServiceEventsAck,
    ServiceEventsParams,
    VmSignInParams,
)
from igni_core.extensions.service_events import ServiceEventInbox
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod


class VmSignIn(Protocol):
    """Takes a person's session for the VM's own credential (a layer's credential
    keeper); answers its state."""

    async def __call__(self, session_token: str, organisation: str | None, /) -> BaseModel: ...


class VmSignInRpc(PoolRpc):
    """``{session_token, organisation?}`` → the credential's state."""

    methods = frozenset({RpcMethod.VM_SIGN_IN})

    NO_SESSION = "vm_sign_in: a producer's VM takes the person's session_token"

    def __init__(self, sign_in: VmSignIn) -> None:
        self._sign_in = sign_in

    @classmethod
    def unmounted(cls) -> list[UnavailableRpc]:
        return [UnavailableRpc(RpcMethod.VM_SIGN_IN, cls.NO_SESSION)]

    async def answer(self, request: msg.RPCRequest) -> PoolAnswer:
        params = VmSignInParams.model_validate(request.args)
        if not params.session_token:
            return PoolAnswer.failed(self.NO_SESSION)
        change = await self._sign_in(params.session_token, params.organisation)
        return PoolAnswer.ok(change, dump_mode="json")


class ServiceEventRpc(PoolRpc):
    """``{events: [...]}`` → what became of each, and which to acknowledge."""

    methods = frozenset({RpcMethod.DELIVER_SERVICE_EVENTS})

    NO_INBOX = "this igni takes no events"

    def __init__(self, inbox: ServiceEventInbox) -> None:
        self._inbox = inbox

    @classmethod
    def unmounted(cls) -> list[UnavailableRpc]:
        return [UnavailableRpc(RpcMethod.DELIVER_SERVICE_EVENTS, cls.NO_INBOX)]

    async def answer(self, request: msg.RPCRequest) -> PoolAnswer:
        try:
            params = ServiceEventsParams.model_validate(request.args)
        except ValidationError as exc:
            return PoolAnswer.failed(f"not events: {exc}")
        receipt = await self._inbox.deliver(params.events)
        return PoolAnswer.ok(ServiceEventsAck.of(receipt), dump_mode="json")
