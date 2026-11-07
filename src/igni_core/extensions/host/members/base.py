"""What every member class shares: its table of tagged members, and the
ways a member reaches igni.

One class per object, one method per member, each tagged with what it
implements (:mod:`.tag`) — the same shape :class:`~igni_core.backend.rpc_handlers.base.RpcHandler`
gives the RPC table, for the same reason: the table is built by walking
the classes, so a member cannot be implemented under a typo, and
:class:`~igni_core.extensions.host.member_table.MemberTable` refuses one
implemented twice.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.tag import MemberBinding, MemberTag
from igni_core.extensions.host.rpc_wire.base import NO_ARGS, RpcInfo
from igni_core.extensions.host.rpc_wire.status import StatusAnswer
from igni_core.extensions.host.services import HostServices
from igni_core.extensions.host.signal_values import Emitter
from igni_core.extensions.host.spec import ErrorCode
from igni_core.protocol.rpc import RpcMethod


class Members:
    """Base: the host's services, and the members this class implements."""

    def __init__(self, host: HostServices) -> None:
        self.host = host
        #: Where a member's signals go: dumped once, at the seam.
        self.signals = Emitter(host.bus.emit)

    def table(self) -> list[MemberBinding]:
        """The member and the bound method, for every tagged method. A key
        that appears twice is :class:`MemberTable`'s to refuse."""
        out: list[MemberBinding] = []
        for name in dir(type(self)):
            if name.startswith("__"):
                continue
            key = MemberTag.of(getattr(type(self), name, None))
            if key is not None:
                out.append(MemberBinding(key=key, impl=getattr(self, name)))
        return out

    async def rpc(self, method: RpcMethod, args: BaseModel = NO_ARGS, *, c: Call) -> Any:
        """An RPC handler, in the caller's session."""
        return await self.rpc_in(method, args, c.session_id)

    async def rpc_in(self, method: RpcMethod, args: BaseModel, session_id: str) -> Any:
        """An RPC handler, in a session named outright."""
        return await self.host.gateway.rpc(method, args.model_dump(), session_id=session_id)

    async def rpc_checked(self, method: RpcMethod, args: BaseModel = NO_ARGS, *, c: Call) -> Any:
        """An RPC handler whose refusal comes back as an ``info`` of type
        ``error``, rather than raised: that answer is a refusal here."""
        info = await self.rpc(method, args, c=c)
        refusal = RpcInfo.from_rpc(info).refusal()
        if refusal is not None:
            raise HostModelError(ErrorCode.REFUSED, refusal)
        return info

    async def read_status(self, c: Call) -> StatusAnswer:
        """``get_status``, in the caller's session, read once. Not named
        ``status``: several objects have a member of that name."""
        return StatusAnswer.from_rpc(await self.rpc(RpcMethod.GET_STATUS, c=c))


__all__ = ["Members"]
