"""``/evals`` slash command implementation.

Extracted from :mod:`igni_core.backend.command_handler` — the
old inline ``_cmd_evals`` body called ``SuiteRunner.run_all``
and formatted results inline. :class:`EvalsCommand` delegates
rendering to :class:`EvalRunView` (see :mod:`schemas_evals`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from igni_core.backend.command_result import CommandResult
from igni_core.backend.schemas_evals import EvalRunView
from igni_core.core.evals.runner import SuiteRunner

if TYPE_CHECKING:
    from igni_core.backend.command_handler import CommandHandler
    from igni_core.core.session import Session


class EvalsCommand:
    """Coordinator for the ``/evals`` slash command."""

    def __init__(self, session: Session) -> None:
        self._session = session

    async def run(self, agent_filter: str | None) -> CommandResult:
        results = await SuiteRunner.run_all(
            pool=self._session.pool,
            settings=self._session.settings,
            project_dir=self._session.project_dir,
            agent_filter=agent_filter,
        )
        return EvalRunView(results=list(results) if results else []).to_command_result()


async def cmd_evals(handler: CommandHandler, args: str) -> CommandResult:
    """Two-line shim for :class:`EvalsCommand`."""
    filt = args.strip() or None
    return await EvalsCommand(handler.session).run(filt)
