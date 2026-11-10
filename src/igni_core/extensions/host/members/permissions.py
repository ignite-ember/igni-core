"""``host.permissions``: what the agent may do, and what a tool call
would be allowed.

The level is one of three words. A fourth is not a level: it is
refused, by name, rather than passed on to an extension that would
have to guess what it meant.
"""

from __future__ import annotations

from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.choice import Choice
from igni_core.extensions.host.members.tag import call, get
from igni_core.extensions.host.rpc_wire.settings import CheckPermissionArgs, SaveRuleArgs
from igni_core.protocol.rpc import RpcMethod


class PermissionLevel(Choice):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"

    @classmethod
    def refusal(cls, value: str) -> str:
        return f"level is allow, ask or deny — not {value!r}"


class PermissionMembers(Members):
    @get("permissions.mode")
    async def mode(self, c: Call) -> str:
        return (await self.read_status(c)).permission_mode

    @call("permissions.check")
    async def check(self, c: Call) -> str:
        args = CheckPermissionArgs(
            tool_name=c.params["tool_name"],
            func_name=c.params["func_name"],
            tool_args=c.params.get("tool_args") or {},
        )
        found = await self.rpc(RpcMethod.CHECK_PERMISSION, args, c=c)
        return PermissionLevel.of(str(found)).value

    @call("permissions.save_rule")
    async def save_rule(self, c: Call) -> None:
        level = PermissionLevel.of(str(c.params["level"]))
        args = SaveRuleArgs(rule=c.params["rule"], level=level.value)
        await self.rpc(RpcMethod.SAVE_PERMISSION_RULE, args, c=c)


__all__ = ["PermissionLevel", "PermissionMembers"]
