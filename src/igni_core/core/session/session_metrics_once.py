"""A run's tokens reach its session's totals once, when the run ends, however often it paused."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, ClassVar

import agno.agent._storage as agent_storage
import agno.team._session as team_session
from agno.run.base import RunStatus


class SessionMetricsOnce:
    """Agno's session-metrics updates, skipped for a run that has not ended."""

    UNFINISHED: ClassVar[frozenset[RunStatus]] = frozenset(
        {RunStatus.pending, RunStatus.running, RunStatus.paused}
    )
    """Statuses of a run that may go on and be persisted again."""
    _installed: ClassVar[bool] = False

    @classmethod
    def install(cls) -> None:
        """Wrap the agent's and the team's update, once per process."""
        if cls._installed:
            return
        agent_storage.update_session_metrics = cls._when_ended(agent_storage.update_session_metrics)
        team_session.update_session_metrics = cls._when_ended(team_session.update_session_metrics)
        cls._installed = True

    @classmethod
    def _when_ended(cls, update: Callable[..., None]) -> Callable[..., None]:
        @functools.wraps(update)
        def counted_when_ended(owner: Any, session: Any, run_response: Any) -> None:
            if run_response.status in cls.UNFINISHED:
                return
            update(owner, session, run_response)

        return counted_when_ended
