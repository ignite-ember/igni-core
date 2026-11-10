"""A team's paused run, continued after a restart with the decisions.

Agno keeps the team's run: members that finished keep their results in
it, and only the paused members are continued before the team leader
goes on (``backend/cold_spawn_resume.py``).
"""

from __future__ import annotations

from typing import Any

from igni_core.core.tools.orchestrate_stream_context import ResumeSpec, StreamContext
from igni_core.core.tools.orchestrate_team_stream import TeamStreamHandler


class ResumedTeamStreamHandler(TeamStreamHandler):
    """A team's paused run, picked up where it stopped."""

    def __init__(self, context: StreamContext, resume: ResumeSpec) -> None:
        super().__init__(context)
        self._resume = resume
        # The team's run, known up front: the continued stream starts
        # with the members' events, whose run ids are theirs.
        self.state.current_run_id = resume.run_id
        self.state.current_session_id = resume.session_id

    def _first_stream(self) -> Any:
        return self._context.runnable.acontinue_run(
            run_id=self._resume.run_id,
            session_id=self._resume.session_id,
            requirements=self._resume.requirements,
            stream=True,
        )


__all__ = ["ResumedTeamStreamHandler"]
