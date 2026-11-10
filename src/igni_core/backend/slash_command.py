"""Shared :class:`SlashCommand` Protocol.

A ``cmd_*`` package may implement its commands as classes satisfying
this Protocol (starting with :mod:`igni_core.backend.cmd_plugin`).
:class:`BuiltinCommandRegistry` accepts such a class as an entry's
target: it constructs it on first use and dispatches to ``run``.

Kept in its own module (not inside the registry file) so any
``cmd_*.py`` package can implement the protocol without importing
the registry back — that would cycle at import time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from igni_core.backend.command_handler import CommandHandler
    from igni_core.backend.command_result import CommandResult


@runtime_checkable
class SlashCommand(Protocol):
    """Minimum surface a slash-command implementation must expose.

    ``name`` is the leading-slash form (``"/plugin"``). ``description``
    feeds :class:`SlashCommandsCatalog`. ``run(handler, args)`` is the
    dispatch entrypoint the registry calls.
    """

    name: str
    description: str

    async def run(self, handler: CommandHandler, args: str) -> CommandResult:
        """Execute the command. ``args`` is the arg suffix after the
        leading slash-name token (already stripped of the command
        name itself)."""
        ...


__all__ = ["SlashCommand"]
