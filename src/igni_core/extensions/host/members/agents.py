"""``host.agents``: the agents, and what becomes of a temporary one."""

from __future__ import annotations

from igni_core.backend.schemas_panels import DiscardEphemeralResult, PromoteEphemeralResult
from igni_core.core.agents.schemas import AgentInfo
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.choice import Choice
from igni_core.extensions.host.members.reads import read_answer, read_answers
from igni_core.extensions.host.members.tag import call, get
from igni_core.extensions.host.rpc_wire.work import AgentNameArgs, PromoteArgs
from igni_core.protocol.rpc import RpcMethod


class PromoteTarget(Choice):
    """Where ``promote_ephemeral`` may keep an agent, as ``/agents promote
    --project | --personal`` and the panel's RPC ``target`` name them."""

    PROJECT = "project"
    PERSONAL = "personal"

    @classmethod
    def refusal(cls, value: str) -> str:
        return (
            f"no promote target {value!r}: project (.igni/agents/, the default) "
            "or personal (.igni/agents.local/)"
        )


class AgentMembers(Members):
    @get("agents.details")
    async def details(self, c: Call) -> list[AgentInfo]:
        return read_answers(
            AgentInfo,
            await self.rpc(RpcMethod.GET_AGENT_DETAILS, c=c),
            method="agents.details",
        )

    @call("agents.promote_ephemeral")
    async def promote(self, c: Call) -> PromoteEphemeralResult:
        target = PromoteTarget.of(c.params.get("target") or PromoteTarget.PROJECT.value)
        args = PromoteArgs(name=c.params["name"], target=target.value)
        return read_answer(
            PromoteEphemeralResult,
            await self.rpc(RpcMethod.PROMOTE_EPHEMERAL_AGENT, args, c=c),
            method="agents.promote_ephemeral",
        )

    @call("agents.discard_ephemeral")
    async def discard(self, c: Call) -> DiscardEphemeralResult:
        args = AgentNameArgs(name=c.params["name"])
        return read_answer(
            DiscardEphemeralResult,
            await self.rpc(RpcMethod.DISCARD_EPHEMERAL_AGENT, args, c=c),
            method="agents.discard_ephemeral",
        )


__all__ = ["AgentMembers", "PromoteTarget"]
