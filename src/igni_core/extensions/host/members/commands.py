"""``host.commands``: the slash commands, the skills, and running one."""

from __future__ import annotations

from pydantic import TypeAdapter

from igni_core.backend.schemas_panels import SlashCommandEntry
from igni_core.backend.schemas_rpc import SkillDefinition
from igni_core.core.skills.parser import SkillInfo
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.reads import read_answers, read_list
from igni_core.extensions.host.members.tag import call, get
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.values import CommandOutcome
from igni_core.protocol.rpc import RpcMethod

#: The commands, whichever kind they are: the ``source`` string picks
#: the variant, and one union names them all.
_COMMANDS = TypeAdapter(list[SlashCommandEntry])

#: The skill names, which are strings and nothing more.
_SKILLS = TypeAdapter(list[str])


class CommandMembers(Members):
    @get("commands.list")
    async def command_list(self, c: Call) -> list[SlashCommandEntry]:
        return read_list(
            _COMMANDS,
            await self.rpc(RpcMethod.GET_SLASH_COMMANDS, c=c),
            method="commands.list",
            what="a slash command",
        )

    @get("commands.skills")
    async def skills(self, c: Call) -> list[str]:
        return read_list(
            _SKILLS,
            await self.rpc(RpcMethod.GET_SKILL_NAMES, c=c),
            method="commands.skills",
            what="a skill name",
        )

    @get("commands.skill_definitions")
    async def skill_definitions(self, c: Call) -> list[SkillDefinition]:
        return read_answers(
            SkillDefinition,
            await self.rpc(RpcMethod.GET_SKILL_DEFINITIONS, c=c),
            method="commands.skill_definitions",
        )

    @get("commands.skill_details")
    async def skill_details(self, c: Call) -> list[SkillInfo]:
        return read_answers(
            SkillInfo,
            await self.rpc(RpcMethod.GET_SKILL_DETAILS, c=c),
            method="commands.skill_details",
        )

    @call("commands.run")
    async def run(self, c: Call) -> CommandOutcome:
        command = str(c.params["command"]).strip()
        if not command.startswith("/"):
            raise HostModelError(ErrorCode.INVALID, f"{command!r} is not a slash command")
        return await self.host.gateway.command(command, session_id=c.session_id)


__all__ = ["CommandMembers"]
