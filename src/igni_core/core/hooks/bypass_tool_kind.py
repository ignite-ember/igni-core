"""What kind of action a tool call is, as Bypass mode's last check sees it.

The guard (:mod:`igni_core.core.hooks.bypass_guard`) treats a shell
command, a file write, a fetch and a file read differently: each has its
own rule, its own subject in the dialog and its own one-line form for
Laya. One :class:`ToolKind` holds all three, so they cannot drift apart.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any

from igni_core.core.hooks.bypass_rules import flag_file_read, flag_shell, flag_url, flag_write

_SHELL_TOOLS = frozenset({"run_shell_command", "bash", "shell"})
_WRITE_TOOLS = frozenset({"edit_file", "edit_file_replace_all", "create_file", "write_file"})
_FETCH_TOOLS = frozenset({"fetch_url", "fetch_json"})
#: Read-only file tools that take a path. They are waved through as read-only,
#: but a read of a *secret* path is exactly the exfil first step that exemption
#: should not cover, so the guard asks :meth:`ToolKind.secret_read` for these.
_READ_FILE_TOOLS = frozenset({"read_file", "search_files"})


class ToolKind(StrEnum):
    """The shape of a tool call: what its rule checks, how it reads, how Laya sees it."""

    SHELL = "shell"
    WRITE = "write"
    FETCH = "fetch"
    READ_FILE = "read_file"
    OTHER = "other"

    @classmethod
    def of(cls, tool_name: str) -> ToolKind:
        if tool_name in _SHELL_TOOLS:
            return cls.SHELL
        if tool_name in _WRITE_TOOLS:
            return cls.WRITE
        if tool_name in _FETCH_TOOLS:
            return cls.FETCH
        if tool_name in _READ_FILE_TOOLS:
            return cls.READ_FILE
        return cls.OTHER

    @property
    def subject(self) -> str:
        """How the dialog names the call: "This command", "This file write", …"""
        match self:
            case ToolKind.SHELL:
                return "This command"
            case ToolKind.WRITE:
                return "This file write"
            case ToolKind.FETCH:
                return "This request"
            case ToolKind.READ_FILE:
                return "This file read"
            case _:
                return "This call"

    def target(self, args: dict[str, Any]) -> str:
        """The one argument the call is about — the command, the path, the URL — or ''."""
        match self:
            case ToolKind.SHELL:
                value = args.get("command") or args.get("cmd")
            case ToolKind.WRITE:
                value = args.get("path") or args.get("file_path")
            case ToolKind.FETCH:
                value = args.get("url")
            case ToolKind.READ_FILE:
                value = args.get("file_path") or args.get("path")
            case _:
                value = None
        return str(value or "")

    def secret_read(self, args: dict[str, Any]) -> str | None:
        """Why reading this path needs a yes, for a file read of a secret; otherwise None."""
        return flag_file_read(self.target(args)) if self is ToolKind.READ_FILE else None

    def rule(self, args: dict[str, Any], project_dir: Path | None) -> str | None:
        """What a shape rule says is dangerous about the call, or None."""
        match self:
            case ToolKind.SHELL:
                return flag_shell(self.target(args), project_dir)
            case ToolKind.WRITE:
                return flag_write(self.target(args), project_dir)
            case ToolKind.FETCH:
                return flag_url(self.target(args))
            case _:
                return None

    def describe(self, tool_name: str, args: dict[str, Any]) -> str:
        """The call as Laya was measured on it: ``Tool: argument``."""
        match self:
            case ToolKind.SHELL:
                return f"Bash: {self.target(args)}"
            case ToolKind.WRITE:
                content = str(args.get("content") or args.get("new_string") or "")[:400]
                return f"Write: {self.target(args)}  content: {content!r}"
            case ToolKind.FETCH:
                return f"WebFetch: {self.target(args)}"
            case ToolKind.READ_FILE:
                return f"Read: {self.target(args)}"
            case _:
                return f"{tool_name}: {json.dumps(args, default=str)[:1500]}"
