"""Which window RPCs are in flight, so the host model can signal a write."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from typing import ClassVar

from igni_core.backend.rpc_router import POOL_LEVEL_RPCS
from igni_core.extensions.host.rpc_write import RpcWrite
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod


class RpcWatch:
    """Which window RPCs are in flight, so a success can be signalled.

    "Every write is a signal": a window's write through an RPC broadcasts
    nothing of its own, so the host learns it succeeded from the reply
    going out and tells
    :meth:`~igni_core.extensions.host.broadcasts.BroadcastSignals.after_rpc`
    the :class:`~igni_core.extensions.host.rpc_write.RpcWrite`.
    """

    LIMIT: ClassVar[int] = 1000

    def __init__(self, after: Callable[[RpcWrite], None]) -> None:
        self._after = after
        self._pending: OrderedDict[str, RpcWrite] = OrderedDict()

    def inbound(self, message: msg.Message) -> None:
        if not isinstance(message, msg.RPCRequest) or not message.id:
            return
        try:
            method = RpcMethod(message.method)
        except ValueError:
            return
        if method in POOL_LEVEL_RPCS:
            return
        self._pending[message.id] = RpcWrite(
            method=method, args=dict(message.args or {}), session_id=message.session_id
        )
        while len(self._pending) > self.LIMIT:
            self._pending.popitem(last=False)

    def outbound(self, message: msg.Message) -> None:
        if not message.id or message.id not in self._pending:
            return
        write = self._pending.pop(message.id)
        if isinstance(message, msg.RPCResponse):
            if message.error:
                return
        elif message.type == "error":
            return
        if not write.session_id:
            write = write.model_copy(update={"session_id": message.session_id})
        self._after(write)


__all__ = ["RpcWatch"]
