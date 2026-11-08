"""Typed view models for the ``/hooks`` slash command's chat output.

Extracted from :mod:`igni_core.backend.command_handler` — the
old ``_cmd_hooks`` inline body built the "## Hooks" markdown
list procedurally. Now a Pydantic view model with
``.to_command_result()`` — mirrors the :mod:`schemas_context`
pattern.
"""

from __future__ import annotations

from pydantic import BaseModel

from igni_core.backend.command_result import CommandResult
from igni_core.core.hooks.schemas import HookDefinition


class HooksReloadResult(BaseModel):
    """Result of :meth:`Session.reload_hooks` — just the count so
    the coordinator renders ``Hooks reloaded. N hook(s) loaded.``
    without knowing the format string.
    """

    count: int

    def to_command_result(self) -> CommandResult:
        return CommandResult.info(f"Hooks reloaded. {self.count} hook(s) loaded.")


class HooksListView(BaseModel):
    """Wraps :attr:`Session.hooks_map` for the ``/hooks list``
    markdown output. Empty map renders as an info result.
    """

    hooks_map: dict[str, list[HookDefinition]]

    def to_command_result(self) -> CommandResult:
        if not self.hooks_map:
            return CommandResult.info("No hooks loaded.")
        lines = "## Hooks\n"
        for event, hook_list in self.hooks_map.items():
            for h in hook_list:
                matcher = f" (matcher: {h.matcher})" if h.matcher else ""
                lines += f"- **{event}**: `{h.command or h.url}`{matcher}\n"
        return CommandResult.markdown(lines)


__all__ = ["HooksReloadResult", "HooksListView"]
