"""The tool call every permission stage, invoker and suffixer looks at."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from agno.tools.function import Function
from agno.tools.toolkit import Toolkit


@dataclass(frozen=True)
class ToolCallContext:
    """One tool call, packed from Agno's untyped ``tool_hook`` arguments.

    ``agent`` or ``team`` is whichever Agno passed as the caller: a tool
    that belongs to a team rather than to one of its agents comes with
    the team.
    """

    name: str
    func: Any  # Callable | None — ``Any`` skips Pydantic introspection.
    args: dict[str, Any]
    agent: Any = None
    team: Any = None

    @classmethod
    def for_check(cls, name: str, args: dict[str, Any]) -> ToolCallContext:
        """A call with no callable and no caller, for the synchronous safety checks."""
        return cls(name=name, func=None, args=args)

    def with_args(self, args: dict[str, Any]) -> ToolCallContext:
        """The same call with other arguments: what an extension's PreToolUse hook changed them to."""
        if args is self.args:
            return self
        return replace(self, args=dict(args))

    @property
    def agent_path(self) -> list[str]:
        """The calling agent's name as an approval prompt's path, or empty."""
        name = getattr(self.agent, "name", None)
        return [name] if name else []

    def built_with_pause(self) -> bool | None:
        """Was this tool built to pause for approval before it runs?

        Agno pauses a call only when its ``Function`` carries
        ``requires_confirmation``, and a paused call reaches the tool hook
        only once someone confirmed it. So ``True`` means an ASK has been
        answered; ``False`` means nobody was asked. ``None`` when the tool
        cannot be found on the agent or team running it.
        """
        for owner in (self.agent, self.team):
            tools = getattr(owner, "tools", None)
            if not isinstance(tools, list):
                continue
            for tool in tools:
                if isinstance(tool, Toolkit):
                    if self.name in (tool.requires_confirmation_tools or []):
                        return True
                    func = tool.functions.get(self.name) or tool.async_functions.get(self.name)
                    if func is not None:
                        return bool(func.requires_confirmation)
                elif isinstance(tool, Function) and tool.name == self.name:
                    return bool(tool.requires_confirmation)
        return None
