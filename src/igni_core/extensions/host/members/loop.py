"""``host.loop``: `/loop`, where it is and what it does next.

``loop.changed`` is emitted for every loop write, the person's and
these, by :class:`~igni_core.extensions.host.broadcasts.rpc_writes.RpcWriteSignals`.
"""

from __future__ import annotations

from igni_core.backend.schemas_loop import LoopStatusSnapshot
from igni_core.core.session.schemas import LoopAdvance
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.reads import read_answer
from igni_core.extensions.host.members.tag import call, get
from igni_core.protocol.rpc import RpcMethod


class LoopMembers(Members):
    @get("loop.status")
    async def status(self, c: Call) -> LoopStatusSnapshot:
        return read_answer(
            LoopStatusSnapshot,
            await self.rpc(RpcMethod.LOOP_STATUS, c=c),
            method="loop.status",
        )

    @call("loop.pause")
    async def pause(self, c: Call) -> bool:
        return bool(await self.rpc(RpcMethod.LOOP_PAUSE, c=c))

    @call("loop.resume")
    async def resume(self, c: Call) -> str:
        return str(await self.rpc(RpcMethod.LOOP_RESUME, c=c) or "")

    @call("loop.cancel_pending")
    async def cancel_pending(self, c: Call) -> bool:
        return bool(await self.rpc(RpcMethod.CANCEL_PENDING_LOOP, c=c))

    @call("loop.pop_pending")
    async def pop_pending(self, c: Call) -> LoopAdvance | None:
        found = await self.rpc(RpcMethod.POP_PENDING_LOOP_ITERATION, c=c)
        if found is None:
            # Nothing was waiting. That is an answer, not a gap.
            return None
        return read_answer(LoopAdvance, found, method="loop.pop_pending")


__all__ = ["LoopMembers"]
