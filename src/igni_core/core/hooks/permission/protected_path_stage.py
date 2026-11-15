"""Defense in depth: no write to a protected path."""

from __future__ import annotations

import fnmatch
import logging
import shlex
from pathlib import PurePosixPath
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

    #: igni's own settings files, protected whatever ``safety.protected_paths``
    #: says. They carry ``permissions`` (and hooks): an agent that could
    #: write them could raise its own permissions — with ordinary edit
    #: approval it wrote ``.igni/settings.local.json`` allowing Bash, and
    #: every later session in the trusted folder ran shell unasked
    #: (campaign R110). Only the person changes these.
    ALWAYS_PROTECTED: ClassVar[tuple[str, ...]] = (
        ".igni/settings*.json",
        ".igni/config*.yaml",
        ".igni/config*.yml",
    )

    def __init__(self, protected_paths: list[str]) -> None:
        self._protected_paths = [*protected_paths, *self.ALWAYS_PROTECTED]

    #: The shell tool: what it writes through a redirect or ``tee`` is gated too.
    SHELL_TOOL: ClassVar[str] = "run_shell_command"

    #: Shell operators whose next word is a file written to.
    _REDIRECTS: ClassVar[frozenset[str]] = frozenset({">", ">>", ">|", "&>", "&>>"})

    @classmethod
    def applies_to(cls, tool_name: str) -> bool:
        """Whether this stage gates ``tool_name``."""
        return tool_name in cls.WRITE_TOOL_FUNCTIONS or tool_name == cls.SHELL_TOOL

    @classmethod
    def shell_write_targets(cls, command: str) -> list[str]:
        """The files a shell command writes through ``>``-style redirects or ``tee``.

        Not every way a command can write a file — ``cp``, ``mv``, ``sed -i``
        are not read — but the two the shell itself spells, which is how
        ``echo hi > x.secret`` wrote a protected file once the call was
        approved (campaign R109). Unparseable text yields nothing here; the
        approval the shell needs anyway still applies.
        """
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            return []
        targets: list[str] = []
        for i, token in enumerate(tokens):
            if token in cls._REDIRECTS and i + 1 < len(tokens):
                targets.append(tokens[i + 1])
            elif token == "tee":
                for arg in tokens[i + 1 :]:
                    if arg in {"|", ";", "&&", "||", "&"} or arg in cls._REDIRECTS:
                        break
                    if not arg.startswith("-"):
                        targets.append(arg)
        return [t for t in targets if t and not t.startswith("&") and t != "/dev/null"]

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
        """Whether ``path`` matches a protected pattern.

        A pattern matches by basename (``.env``), in full
        (``**/credentials.json``), or against the path's trailing
        components, so ``terraform/*.tfstate`` covers the same file spelt
        ``./terraform/x.tfstate`` or ``/abs/project/terraform/x.tfstate``.
        A pattern ending in ``/`` names a directory (``keys/``): everything
        under a directory of that name is protected.
        """
        parts = [
            part for part in PurePosixPath(path.replace("\\", "/")).parts if part not in ("/", ".")
        ]
        tails = ["/".join(parts[i:]) for i in range(len(parts))]
        directories = parts[:-1]
        for pattern in protected_patterns:
            if pattern.endswith("/"):
                name = pattern.rstrip("/")
                if name and any(
                    fnmatch.fnmatch("/".join(directories[i:j]), name)
                    for j in range(1, len(directories) + 1)
                    for i in range(j)
                ):
                    return True
                continue
            if fnmatch.fnmatch(path, pattern) or any(
                fnmatch.fnmatch(tail, pattern) for tail in tails
            ):
                return True
        return False

    def check_sync(self, ctx: ToolCallContext) -> SafetyCheckResult:
        """Does this call write to a protected path? Quiet: :meth:`evaluate` logs."""
        if not self._protected_paths or not self.applies_to(ctx.name):
            return SafetyCheckResult.no_block()
        if ctx.name == self.SHELL_TOOL:
            command = ctx.args.get("command")
            for target in self.shell_write_targets(command if isinstance(command, str) else ""):
                if self.matches_pattern(target, self._protected_paths):
                    return SafetyCheckResult.block(
                        f"Blocked: '{target}' is a protected path and cannot be written to."
                    )
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
                "Protected path blocked: %s via %s",
                self.target_path(ctx.args) or ctx.args.get("command", ""),
                ctx.name,
            )
            return Block(result.block_message)
        return Continue()
