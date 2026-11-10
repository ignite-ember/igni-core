"""A sub-agent's paused run, continued instead of started.

Used after a restart (``backend/cold_spawn_resume.py``) for two kinds of
paused run: one Agno stored, paused for approvals the person has now
answered (continued by ``run_id``), and one rebuilt from a call that
was waiting on another agent (continued from ``run_response`` with that
agent's answer as the call's result).

Everything after the first stream — another pause, the final answer —
is the parent's, so a continued run streams its progress and can be
paused again like a live one.
"""

from __future__ import annotations

from typing import Any

from igni_core.core.tools.orchestrate_agent_stream import SubAgentStreamHandler
from igni_core.core.tools.orchestrate_stream_context import ResumeSpec, StreamContext


class ResumedSubAgentStreamHandler(SubAgentStreamHandler):
    """A specialist's paused run, picked up where it stopped."""

    def __init__(self, context: StreamContext, resume: ResumeSpec) -> None:
        super().__init__(context)
        self._resume = resume
        # The run id latches from the continued run's events, which
        # registers it for cancellation like any other.
        self.state.current_session_id = resume.session_id

    def _first_stream(self) -> Any:
        if self._resume.run_response is not None:
            return self._context.runnable.acontinue_run(
                run_response=self._resume.run_response,
                session_id=self._resume.session_id,
                requirements=self._resume.requirements,
                stream=True,
            )
        return self._context.runnable.acontinue_run(
            run_id=self._resume.run_id,
            session_id=self._resume.session_id,
            requirements=self._resume.requirements,
            stream=True,
        )


__all__ = ["ResumedSubAgentStreamHandler"]
