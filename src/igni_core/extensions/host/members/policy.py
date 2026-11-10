"""``host.policy``: the group policy in force."""

from __future__ import annotations

from igni_core.backend.schemas_rpc import GroupPolicyPackResult
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.reads import read_answer
from igni_core.extensions.host.members.tag import get
from igni_core.protocol.rpc import RpcMethod


class PolicyMembers(Members):
    @get("policy.pack")
    async def pack(self, c: Call) -> GroupPolicyPackResult:
        return read_answer(
            GroupPolicyPackResult,
            await self.rpc(RpcMethod.GET_GROUP_POLICY, c=c),
            method="policy.pack",
        )


__all__ = ["PolicyMembers"]
