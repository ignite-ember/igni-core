"""A run's tokens reach its session's totals once, when the run ends.

Agno adds a run's metrics to ``session_data["session_metrics"]`` each time
it persists the run, and it persists a run at every pause as well as at
its end. A run's metrics are its running total, so a run that paused N
times was added N + 1 times, each time with everything so far. In Bypass
every shell call pauses — the shell is built with ``requires_confirmation``
and the policy answers the pause — so a 54-call turn of 2.4M input tokens
put 56.8M on its session.

A run that is paused, running or pending is not counted; it is counted
once it completes, errors or is cancelled, with its whole total.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, ClassVar

import agno.agent._storage as agent_storage
import agno.team._session as team_session
from agno.run.base import RunStatus


class SessionMetricsOnce:
    """Agno's session-metrics updates, skipped for a run that has not ended."""

    #: A run in one of these may still go on, and be persisted again.
    UNFINISHED: ClassVar[frozenset[RunStatus]] = frozenset(
        {RunStatus.pending, RunStatus.running, RunStatus.paused}
    )
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
