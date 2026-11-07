"""What every member class shares: the call it is handed, the tags that
say which member a method implements, and the ways a member reaches igni.

One class per object, one method per member, each tagged with what it
implements — the same shape :class:`~igni_core.backend.rpc_handlers.base.RpcHandler`
gives the RPC table, for the same reason: the table is built by walking
the classes, so a member cannot be implemented under a typo, and
:class:`~igni_core.extensions.host.member_table.MemberTable` refuses one
implemented twice.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.members.answers import RpcInfo
from igni_core.extensions.host.services import HostServices
from igni_core.extensions.host.spec import ErrorCode
from igni_core.protocol.rpc import RpcMethod

#: The attribute a tagged method carries: its ``(op, member path)``.
_TAG = "_host_member"


class Call(BaseModel):
    """One request, resolved: who asks, about which window and session."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    extension_id: str
    grants: HostGrants
    #: The window a per-window member is about, or the caller's window.
    window: str | None
    #: The session the caller's chat is: its window's, else the default —
    #: or the one ``host.sessions.chat(id)`` named.
    session_id: str
    params: dict[str, Any]
    value: Any = None
    #: Whether ``session_id`` is the session on screen. ``False`` only
    #: for ``host.sessions.chat(id)`` of another session.
    on_screen: bool = True


Impl = Callable[[Call], Awaitable[Any]]

_F = TypeVar("_F", bound=Callable[..., Any])


def get(path: str) -> Callable[[_F], _F]:
    return _tag("get", path)


def put(path: str) -> Callable[[_F], _F]:
    return _tag("set", path)


def call(path: str) -> Callable[[_F], _F]:
    return _tag("call", path)


def _tag(op: str, path: str) -> Callable[[_F], _F]:
    def decorate(fn: _F) -> _F:
        setattr(fn, _TAG, (op, path))
        return fn

    return decorate


class Members:
    """Base: the host's services, and the members this class implements."""

    def __init__(self, host: HostServices) -> None:
        self.host = host

    def table(self) -> list[tuple[tuple[str, str], Impl]]:
        """``((op, path), bound method)`` for every tagged method. A key
        that appears twice is :class:`MemberTable`'s to refuse."""
        out: list[tuple[tuple[str, str], Impl]] = []
        for name in dir(type(self)):
            if name.startswith("__"):
                continue
            key = getattr(getattr(type(self), name, None), _TAG, None)
            if key is not None:
                out.append((key, getattr(self, name)))
        return out

    async def rpc(self, method: RpcMethod, args: BaseModel | None = None, *, c: Call) -> Any:
        """An RPC handler, in the caller's session."""
        return await self.rpc_in(method, args, c.session_id)

    async def rpc_in(self, method: RpcMethod, args: BaseModel | None, session_id: str) -> Any:
        """An RPC handler, in a session named outright."""
        payload = {} if args is None else args.model_dump()
        return await self.host.gateway.rpc(method, payload, session_id=session_id)

    async def rpc_checked(
        self, method: RpcMethod, args: BaseModel | None = None, *, c: Call
    ) -> Any:
        """An RPC handler whose refusal comes back as an ``info`` of type
        ``error``, rather than raised: that answer is a refusal here."""
        info = await self.rpc(method, args, c=c)
        refusal = RpcInfo.from_rpc(info).refusal()
        if refusal is not None:
            raise HostModelError(ErrorCode.REFUSED, refusal)
        return info

    @staticmethod
    def window(c: Call) -> str:
        """The window a call is about; a member that needs one has none
        to act on when no igni window is open."""
        if c.window is None:
            raise HostModelError(ErrorCode.NOT_FOUND, "no igni window is open")
        return c.window


__all__ = ["Call", "Impl", "Members", "call", "get", "put"]
