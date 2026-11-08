"""Defense in depth: no write to a protected path."""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import Any, ClassVar

from igni_core.core.hooks.permission.context import ToolCallContext
from igni_core.core.hooks.permission.outcome import Block, Continue, StageOutcome
from igni_core.core.hooks.permission.stage import PermissionStage
from igni_core.core.hooks.schemas import SafetyCheckResult

logger = logging.getLogger(__name__)

#: The argument names a write tool's target path may arrive under, in
#: lookup order: ``file_path`` for igni's editors and notebook writers,
#: ``file_name`` for agno's ``FileTools.save_file``, and ``path`` for the
#: next write tool that needs gating. Public so a test can hold them
#: against the real tool signatures.
PATH_ARG_KEYS: tuple[str, ...] = ("file_path", "file_name", "path")


class ProtectedPathStage(PermissionStage):
    """Blocks writes to protected paths.

    Always runs — a ``PreToolUse`` ``allow`` cannot unlock it, matching
    CC's bypass-resistant scoped-deny threat model.
    """

    #: The file-toolkit functions that write to the filesystem.
    WRITE_TOOL_FUNCTIONS: ClassVar[frozenset[str]] = frozenset(
        {
            "save_file",
            "edit_file",
            "edit_file_replace_all",
            "create_file",
            "notebook_edit_cell",
            "notebook_add_cell",
            "notebook_remove_cell",
        }
    )

    def __init__(self, protected_paths: list[str]) -> None:
        self._protected_paths = protected_paths

    @classmethod
    def applies_to(cls, tool_name: str) -> bool:
        """Whether this stage gates ``tool_name``."""
        return tool_name in cls.WRITE_TOOL_FUNCTIONS

    @staticmethod
    def target_path(args: dict[str, Any]) -> str:
        """The path a write tool is about to write to, or ``""``."""
        for key in PATH_ARG_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    @staticmethod
    def matches_pattern(path: str, protected_patterns: list[str]) -> bool:
        """Whether ``path`` matches a pattern by basename (``.env``) or in full
        (``**/credentials.json``)."""
        filename = Path(path).name
        return any(
            fnmatch.fnmatch(filename, pattern) or fnmatch.fnmatch(path, pattern)
            for pattern in protected_patterns
        )

    def check_sync(self, ctx: ToolCallContext) -> SafetyCheckResult:
        """Does this call write to a protected path? Quiet: :meth:`evaluate` logs."""
        if not self._protected_paths or not self.applies_to(ctx.name):
            return SafetyCheckResult.no_block()
        file_path = self.target_path(ctx.args)
        if file_path and self.matches_pattern(file_path, self._protected_paths):
            return SafetyCheckResult.block(
                f"Blocked: '{file_path}' is a protected path and cannot be written to."
            )
        return SafetyCheckResult.no_block()

    async def evaluate(self, ctx: ToolCallContext) -> StageOutcome:
        result = self.check_sync(ctx)
        if result.blocked:
            logger.warning(
                "Protected path blocked: %s via %s", self.target_path(ctx.args), ctx.name
            )
            return Block(result.block_message)
        return Continue()
