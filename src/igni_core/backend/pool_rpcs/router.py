"""The pool-level calls, answered before any session hears them.

One table, keyed by method, of the handler that answers it. Every slot
starts filled: a method whose collaborator is built after the
orchestrator (the host model, the page tokens, the event inbox, the VM's
credential keeper) answers why it cannot until that handler is mounted.
"""

from __future__ import annotations

from typing import TypeGuard

from igni_core.backend.pool_replier import PoolReplier
from igni_core.backend.pool_rpcs.approver_tokens import ApproverTokenRpc
from igni_core.backend.pool_rpcs.base import PoolRpc
from igni_core.backend.pool_rpcs.host_model import HostModelRpcs
from igni_core.backend.pool_rpcs.page_token import PageTokenRpc
from igni_core.backend.pool_rpcs.platform_calls import ServiceEventRpc, VmSignInRpc
from igni_core.protocol import messages as msg
from igni_core.protocol.messages import Message
from igni_core.protocol.rpc import RpcMethod


class PoolRpcRouter:
    """Which handler answers each pool-level method, and the connection it answers on."""

    def __init__(self, replier: PoolReplier) -> None:
        self._replier = replier
        self._table: dict[RpcMethod, PoolRpc] = {}
        for placeholder in (
            *HostModelRpcs.unmounted(),
            *PageTokenRpc.unmounted(),
            *VmSignInRpc.unmounted(),
            *ServiceEventRpc.unmounted(),
        ):
            self.mount(placeholder)
        self.mount(ApproverTokenRpc(None))

    @property
    def replier(self) -> PoolReplier:
        return self._replier

    def mount(self, rpc: PoolRpc) -> None:
        """``rpc`` answers its methods from now on."""
        for method in rpc.methods:
            self._table[method] = rpc

    def handles(self, message: Message) -> TypeGuard[msg.RPCRequest]:
        return isinstance(message, msg.RPCRequest) and message.method in self._table

    async def handle(self, request: msg.RPCRequest) -> None:
        await self._table[RpcMethod(request.method)].handle(request, self._replier)
