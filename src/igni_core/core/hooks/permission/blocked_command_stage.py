"""Defense in depth: no shell command on the configured deny list."""

from __future__ import annotations

import logging
from typing import ClassVar

from igni_core.core.config.permission_eval.catalog import BashCommand
from igni_core.core.hooks.permission.context import ToolCallContext
from igni_core.core.hooks.permission.outcome import Block, Continue, StageOutcome
from igni_core.core.hooks.permission.stage import PermissionStage
from igni_core.core.hooks.schemas import SafetyCheckResult

logger = logging.getLogger(__name__)

#: The argument names :meth:`BashCommand.from_args` reads the command
#: from, in its order. Public so a test can hold them against the real
#: shell tool's signature.
SHELL_ARG_KEYS: tuple[str, ...] = ("command", "args")


class BlockedCommandStage(PermissionStage):
    """Blocks shell commands containing a configured pattern.

    Always runs — same threat model as :class:`ProtectedPathStage`.
    """

    #: The functions that spawn shell commands.
    SHELL_TOOL_FUNCTIONS: ClassVar[frozenset[str]] = frozenset({"run_shell_command"})

    def __init__(self, blocked_commands: list[str]) -> None:
        self._blocked_commands = blocked_commands

    @classmethod
    def applies_to(cls, tool_name: str) -> bool:
        """Whether this stage gates ``tool_name``."""
        return tool_name in cls.SHELL_TOOL_FUNCTIONS

    def check_sync(self, ctx: ToolCallContext) -> SafetyCheckResult:
        """Does this shell call contain a blocked pattern? Quiet: :meth:`evaluate` logs."""
        if not self._blocked_commands or not self.applies_to(ctx.name):
            return SafetyCheckResult.no_block()
        command = BashCommand.from_args(ctx.args).command
        for blocked in self._blocked_commands:
            if blocked in command:
                return SafetyCheckResult.block(
                    f"Blocked: command matches blocked pattern '{blocked}'."
                )
        return SafetyCheckResult.no_block()

    async def evaluate(self, ctx: ToolCallContext) -> StageOutcome:
        result = self.check_sync(ctx)
        if result.blocked:
            logger.warning("Blocked command: %s", BashCommand.from_args(ctx.args).command)
            return Block(result.block_message)
        return Continue()
