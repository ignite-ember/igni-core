"""A team cut off by a restart is continued, not started again.

``ResumedTeamStreamHandler`` knows the team's run up front, so the
continued stream's member events carry the team's run id, and its first
stream is ``acontinue_run`` on that run with the decisions, rather than
a new run of the task.
"""

from __future__ import annotations

from typing import Any

import pytest

from igni_core.core.tools.orchestrate_resumed_team_stream import ResumedTeamStreamHandler
from igni_core.core.tools.orchestrate_stream_context import ResumeSpec, StreamContext


class _Team:
    def __init__(self, error: Exception | None = None) -> None:
        self.continued: list[dict[str, Any]] = []
        self.started: list[Any] = []
        self.error = error

    def acontinue_run(self, **kwargs: Any) -> Any:
        self.continued.append(kwargs)
        if self.error is not None:
            raise self.error

        async def stream():
            return
            yield

        return stream()

    def arun(self, *args: Any, **kwargs: Any) -> Any:
        self.started.append((args, kwargs))
        raise AssertionError("a resumed team must not start a new run")


def _handler(team: _Team, requirements: list[Any]) -> ResumedTeamStreamHandler:
    return ResumedTeamStreamHandler(
        StreamContext(runnable=team, task="the task", agent_path=["team(coordinate:a)"]),
        ResumeSpec(run_id="run-9", session_id="sess-9", requirements=requirements),
    )


def test_the_teams_run_is_known_before_any_event_arrives():
    handler = _handler(_Team(), [])

    assert handler.state.current_run_id == "run-9"
    assert handler.state.current_session_id == "sess-9"


def test_the_first_stream_continues_that_run_with_the_decisions():
    team = _Team()
    decisions = [object(), object()]

    stream = _handler(team, decisions)._first_stream()

    assert stream is not None
    assert team.continued == [
        {"run_id": "run-9", "session_id": "sess-9", "requirements": decisions, "stream": True}
    ]
    assert team.started == []


def test_a_continuation_with_no_requirements_still_continues():
    team = _Team()

    _handler(team, [])._first_stream()

    assert team.continued[0]["requirements"] == []


def test_a_run_agno_cannot_find_is_the_callers_error():
    team = _Team(ValueError("No runs found for run ID run-9"))

    with pytest.raises(ValueError, match="No runs found"):
        _handler(team, [])._first_stream()


async def test_running_it_drives_the_continuation_not_a_fresh_run():
    team = _Team()
    handler = _handler(team, ["decision"])

    await handler.run()

    assert len(team.continued) == 1
    assert team.started == []
