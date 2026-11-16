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
from igni_core.core.evals.loader import EvalSuite
from igni_core.core.evals.runner import SuiteRunner

if TYPE_CHECKING:
    from igni_core.backend.command_handler import CommandHandler
    from igni_core.core.session import Session


class EvalsCommand:
    """Coordinator for the ``/evals`` slash command."""

    def __init__(self, session: Session) -> None:
        self._session = session

    async def run(self, agent_filter: str | None, case_filter: str | None = None) -> CommandResult:
        results = await SuiteRunner.run_all(
            pool=self._session.pool,
            settings=self._session.settings,
            project_dir=self._session.project_dir,
            agent_filter=agent_filter,
            case_filter=case_filter,
        )
        if not results and (agent_filter or case_filter):
            missed = self._what_the_filter_missed(agent_filter, case_filter)
            if missed is not None:
                return CommandResult.info(missed)
        return EvalRunView(results=list(results) if results else []).to_command_result()

    def _what_the_filter_missed(self, agent: str | None, case: str | None) -> str | None:
        """Name what exists when suites are there and the filter matched none.

        ``None`` when there are no suites at all — the view says where to
        add them.
        """
        suites = EvalSuite.load_all(
            self._session.project_dir, SuiteRunner.evals_config(self._session.settings)
        )
        if not suites:
            return None
        if agent and not any(s.agent == agent for s in suites):
            names = ", ".join(sorted({s.agent for s in suites}))
            return f"No eval suite for agent {agent!r}. Suites: {names}."
        cases = sorted({c.name for s in suites if not agent or s.agent == agent for c in s.cases})
        return f"No eval case {case!r}. Cases: {', '.join(cases) or '(none)'}."


#: ``/evals`` forms EVALS.md describes that igni has not built.
_NOT_BUILT: tuple[str, ...] = ("diff", "baseline")


async def cmd_evals(handler: CommandHandler, args: str) -> CommandResult:
    """``/evals [run] [<agent>[.<case>]]``.

    ``run`` is optional, so the form EVALS.md documents and the bare one
    DEVELOPMENT.md documents both work. The forms igni has not built
    (``--changed``, ``--orchestrator``, ``diff``, ``baseline``) say so
    instead of reading as an agent name that matches nothing.
    """
    words = args.split()
    if words and words[0] == "run":
        words = words[1:]
    if (words and words[0] in _NOT_BUILT) or any(w.startswith("--") for w in words):
        return CommandResult.info(
            f"/evals {args.strip()} is not available. "
            "Run /evals, /evals <agent> or /evals <agent>.<case>."
        )
    agent, _, case = (words[0] if words else "").partition(".")
    return await EvalsCommand(handler.session).run(agent or None, case or None)
