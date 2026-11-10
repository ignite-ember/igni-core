"""The built-in toolkit names the main agent always gets."""

from __future__ import annotations

from typing import ClassVar

from igni_core.core.config.schemas.permissions import PermissionsConfig
from igni_core.core.tools.registry import ToolRegistry


class MainToolSet:
    """The main agent's built-in tool names, honouring the web permissions."""

    # The shell-first core. Bash covers search/find/list/read (``rg``,
    # ``find``, ``cat``); Edit/Write stay because shell rewrites (``sed -i``,
    # here-docs) are fragile, and the Grep/Glob/Read/LS toolkits overlapped
    # with the shell and confused the model (CLAUDE_CODE_PARITY.md row 22).
    CORE: ClassVar[tuple[str, ...]] = (
        "Write",
        "Edit",
        "Bash",
        "Schedule",
        "NotebookEdit",
    )

    def __init__(self, permissions: PermissionsConfig) -> None:
        self._permissions = permissions

    def names(self, registry: ToolRegistry) -> list[str]:
        names = list(self.CORE)
        if self._permissions.web_search != "deny" and self._resolvable(registry, "WebSearch"):
            names.append("WebSearch")
        if self._permissions.web_fetch != "deny" and self._resolvable(registry, "WebFetch"):
            names.append("WebFetch")
        return names

    @staticmethod
    def _resolvable(registry: ToolRegistry, name: str) -> bool:
        try:
            registry.resolve([name])
        except (ImportError, ValueError):
            return False
        return True


__all__ = ["MainToolSet"]
