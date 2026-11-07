"""``/config`` slash command implementation.

Extracted from :mod:`igni_core.backend.command_handler` — the
old inline ``_cmd_config`` body assembled a 20-line markdown
template inside the handler. :class:`ConfigCommand` now delegates
to :class:`ConfigView` (see :mod:`schemas_config`) so the template
lives in one place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from igni_core.backend.command_result import CommandResult
from igni_core.backend.schemas_config import ConfigView

if TYPE_CHECKING:
    from igni_core.backend.command_handler import CommandHandler
    from igni_core.core.session import Session


class ConfigCommand:
    """Coordinator for the ``/config`` slash command."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def show(self) -> CommandResult:
        return ConfigView.from_session(self._session).to_command_result()


async def cmd_config(handler: CommandHandler, _args: str) -> CommandResult:
    """Two-line shim for :class:`ConfigCommand`."""
    return ConfigCommand(handler.session).show()
