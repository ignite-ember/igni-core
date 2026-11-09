"""Skill listing + definition + details RPC handlers."""

from __future__ import annotations

from igni_core.backend.rpc_binding import NoArgs, rpc
from igni_core.backend.rpc_handlers.base import RpcHandler
from igni_core.backend.schemas_rpc import SkillDefinition
from igni_core.core.skills.parser import SkillInfo
from igni_core.protocol.rpc import RpcMethod, RpcScope


class SkillsRpcHandler(RpcHandler):
    """Names / full definitions / detailed info about the skills the
    backend has loaded for this session."""

    @rpc(RpcMethod.GET_SKILL_NAMES, args=NoArgs, scope=RpcScope.SESSION)
    def get_skill_names(self, args: NoArgs) -> list[str]:
        """List the names of the skills this session has loaded.

        Takes no arguments. Returns the names, each usable as ``/<name>``.
        """
        return self._ctx.backend.skill_names

    @rpc(RpcMethod.GET_SKILL_DEFINITIONS, args=NoArgs, scope=RpcScope.SESSION)
    async def get_skill_definitions(self, args: NoArgs) -> list[SkillDefinition]:
        """List this session's skills with what each tells the agent.

        Takes no arguments. Returns each skill's ``name``, ``description``
        and ``prompt`` (the skill's body).
        """
        pool = self._ctx.backend.get_skill_pool()
        return [
            SkillDefinition(
                name=s.name,
                description=s.description,
                prompt=s.body,
            )
            for s in pool.list_skills()
        ]

    @rpc(RpcMethod.GET_SKILL_DETAILS, args=NoArgs, scope=RpcScope.SESSION)
    def get_skill_details(self, args: NoArgs) -> list[SkillInfo]:
        """Describe each of this session's skills in full.

        Takes no arguments. Returns each skill's name, description,
        version, category, argument hint, the context and agent it runs
        in, whether a person can invoke it, its body and its folder.
        """
        return self._ctx.backend.get_skill_details()
