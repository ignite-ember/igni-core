"""The raw Agno tool name → display name catalog.

Owns the map that used to sit at module scope in
:mod:`igni_core.protocol.agno_tool_formatter` and be reached through
a process-wide registry singleton. The map is immutable — a
:class:`~types.MappingProxyType` behind a class constant — so it is
shared as data, not as mutable module-level state: there is nothing
to build lazily, nothing to invalidate, and no instance to inject.

The catalog lives apart from the formatter hierarchy on purpose:
``permission_eval`` needs the names to invert a permission rule
(``Bash → {run_shell_command, ...}``) and must not pull the
arg-formatting machinery into its import path.

Public surface:

* :meth:`ToolFriendlyNames.friendly_name` — one raw name → display
  name, with a fallback for tools the catalog doesn't know.
* :meth:`ToolFriendlyNames.all_names` — the whole catalog as a
  read-only mapping.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import ClassVar


class ToolFriendlyNames:
    """Immutable catalog of raw Agno tool names → display names."""

    # Snake_case Agno tool name → CC-style display name shown in the
    # tool-call header.
    _NAMES: ClassVar[Mapping[str, str]] = MappingProxyType(
        {
            "read_file": "Read",
            "save_file": "Write",
            "edit_file": "Edit",
            "edit_file_replace_all": "Edit",
            "create_file": "Write",
            "run_shell_command": "Bash",
            "grep": "Grep",
            "grep_files": "Grep",
            "grep_count": "Grep",
            "glob_files": "Glob",
            "list_files": "LS",
            # ``web_search`` / ``search_news`` are what ``WebSearchToolSpec``
            # registers; the ``duckduckgo_*`` spellings are what Agno's toolkit
            # was called before the rename. ``FriendlyToolNameResolver`` inverts
            # this map, so while it carried only the old names a rule reading
            # ``deny: ["WebSearch"]`` resolved to two functions nothing registers
            # and denied nothing — measured: the same rule returned ALLOW for
            # ``web_search`` and DENY for ``duckduckgo_search``. The old names stay
            # so a config written against them keeps working.
            "web_search": "WebSearch",
            "search_news": "WebSearch",
            "duckduckgo_search": "WebSearch",
            "duckduckgo_news": "WebSearch",
            "fetch_url": "WebFetch",
            "fetch_json": "WebFetch",
            "run_python_code": "Python",
            "spawn_agent": "Agent",
            "spawn_team": "Team",
            "delegate_task_to_member": "Delegate",
            "delegate_task_to_members": "Delegate",
            "search_knowledge_base": "Knowledge",
            "update_user_memory": "Memory",
            "schedule_task": "Schedule",
            "list_scheduled_tasks": "Schedule",
            "cancel_scheduled_task": "Schedule",
        }
    )

    @classmethod
    def friendly_name(cls, tool_name: str, default: str | None = None) -> str:
        """Look up the display name for a raw Agno tool name.

        Unknown tools fall back to ``default`` (or the raw name when
        ``default`` is None) so the TUI never shows an empty pill for
        a custom tool.
        """
        if default is None:
            default = tool_name
        return cls._NAMES.get(tool_name, default)

    @classmethod
    def all_names(cls) -> Mapping[str, str]:
        """Read-only view of the whole ``raw name → display name`` map.

        The proxy prevents accidental mutation — the catalog is shared
        by every caller, so nobody gets to write to it.
        """
        return cls._NAMES


__all__ = ["ToolFriendlyNames"]
