"""Typed view models for the ``/hooks`` slash command's chat output.

Extracted from :mod:`igni_core.backend.command_handler` — the
old ``_cmd_hooks`` inline body built the "## Hooks" markdown
list procedurally. Now a Pydantic view model with
``.to_command_result()`` — mirrors the :mod:`schemas_context`
pattern.
"""

from __future__ import annotations

from typing import ClassVar

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
                lines += f"- **{event}**: {self._what_it_does(h)}{matcher}\n"
        return CommandResult.markdown(lines)

    #: How much of a ``prompt`` hook's text the list shows.
    PROMPT_PREVIEW: ClassVar[int] = 60

    @classmethod
    def _what_it_does(cls, hook: HookDefinition) -> str:
        """One line per handler type: a ``prompt`` or ``mcp_tool`` hook has
        neither a command nor a url, and used to list as empty backticks."""
        if hook.command:
            return f"`{hook.command}`"
        if hook.url:
            return f"`{hook.url}`"
        if hook.mcp_server or hook.mcp_tool:
            return f"mcp: `{hook.mcp_server}/{hook.mcp_tool}`"
        if hook.text:
            text = " ".join(hook.text.split())
            if len(text) > cls.PROMPT_PREVIEW:
                text = text[: cls.PROMPT_PREVIEW - 1] + "…"
            return f'prompt: "{text}"'
        return f"({hook.type or 'no handler'})"


__all__ = ["HooksReloadResult", "HooksListView"]
