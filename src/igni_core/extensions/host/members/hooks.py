"""``host.hooks``: the hooks igni has, and the reload that reads them again."""

from __future__ import annotations

from igni_core.backend.schemas_panels import HookEntryView
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.reads import read_answers
from igni_core.extensions.host.members.tag import call, get
from igni_core.protocol.rpc import RpcMethod


class HookMembers(Members):
    @get("hooks.details")
    async def details(self, c: Call) -> list[HookEntryView]:
        return read_answers(
            HookEntryView,
            await self.rpc(RpcMethod.GET_HOOKS_DETAILS, c=c),
            method="hooks.details",
        )

    @call("hooks.reload")
    async def reload(self, c: Call) -> None:
        await self.rpc(RpcMethod.RELOAD_HOOKS, c=c)

    @call("hooks.fire_session_start")
    async def fire_session_start(self, c: Call) -> None:
        await self.rpc(RpcMethod.FIRE_SESSION_START_HOOK, c=c)


__all__ = ["HookMembers"]
