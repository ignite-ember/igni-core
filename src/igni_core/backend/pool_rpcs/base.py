"""One pool-level call's handler, and the placeholder for one nothing has mounted."""

from __future__ import annotations

from abc import ABC, abstractmethod

from igni_core.backend.pool_replier import PoolReplier
from igni_core.backend.schemas_pool import PoolAnswer
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod


class PoolRpc(ABC):
    """Answers the pool-level methods in :attr:`methods`, holding only what it needs."""

    methods: frozenset[RpcMethod]

    @abstractmethod
    async def answer(self, request: msg.RPCRequest) -> PoolAnswer:
        """The result, or the reason there is none."""

    async def handle(self, request: msg.RPCRequest, replier: PoolReplier) -> None:
        """Answer ``request`` on the connection it came on."""
        await replier.reply(request, await self.answer(request))


class UnavailableRpc(PoolRpc):
    """A method this backend has nothing to answer with (yet), and why."""

    def __init__(self, method: RpcMethod, reason: str) -> None:
        self.methods = frozenset({method})
        self._reason = reason

    async def answer(self, request: msg.RPCRequest) -> PoolAnswer:
        return PoolAnswer.failed(self._reason)
