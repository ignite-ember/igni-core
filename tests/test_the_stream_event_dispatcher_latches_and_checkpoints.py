"""What the run's streaming loop does for each event it yields.

A top-level run's input tokens are latched onto the session's context
budget (a sub-agent's, or none, is not); a finished or failed tool call
takes one checkpoint of the team; every other event does nothing. A
checkpoint that cannot be saved is the checkpointer's to swallow, so the
stream never sees it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.backend.session_checkpointer import SessionCheckpointer
from igni_core.backend.stream_event_dispatcher import StreamEventDispatcher
from igni_core.core.session.compaction.context_budget import ContextBudget
from igni_core.protocol import messages as msg


class _Session:
    def __init__(self) -> None:
        self.context_budget = ContextBudget(window=200_000)


def _dispatcher(checkpoints: list[Any]) -> tuple[StreamEventDispatcher, _Session]:
    session = _Session()

    async def checkpoint(team: Any) -> None:
        checkpoints.append(team)

    return StreamEventDispatcher(session, checkpoint), session  # type: ignore[arg-type]


async def test_a_top_level_run_latches_its_input_tokens() -> None:
    dispatcher, session = _dispatcher([])

    await dispatcher.handle(msg.RunCompleted(input_tokens=1234), object())  # type: ignore[arg-type]

    assert session.context_budget.last_input_tokens == 1234


async def test_a_later_run_replaces_the_latched_count() -> None:
    dispatcher, session = _dispatcher([])

    await dispatcher.handle(msg.RunCompleted(input_tokens=1000), object())  # type: ignore[arg-type]
    await dispatcher.handle(msg.RunCompleted(input_tokens=400), object())  # type: ignore[arg-type]

    assert session.context_budget.last_input_tokens == 400


async def test_a_sub_agent_completion_does_not_latch() -> None:
    dispatcher, session = _dispatcher([])
    await dispatcher.handle(msg.RunCompleted(input_tokens=1000), object())  # type: ignore[arg-type]

    sub = msg.RunCompleted(input_tokens=77, parent_run_id="parent")
    await dispatcher.handle(sub, object())  # type: ignore[arg-type]

    assert session.context_budget.last_input_tokens == 1000


async def test_a_run_with_no_input_tokens_does_not_latch() -> None:
    dispatcher, session = _dispatcher([])
    await dispatcher.handle(msg.RunCompleted(input_tokens=1000), object())  # type: ignore[arg-type]

    await dispatcher.handle(msg.RunCompleted(input_tokens=0), object())  # type: ignore[arg-type]

    assert session.context_budget.last_input_tokens == 1000


async def test_a_run_completing_takes_no_checkpoint() -> None:
    checkpoints: list[Any] = []
    dispatcher, _ = _dispatcher(checkpoints)

    await dispatcher.handle(msg.RunCompleted(input_tokens=5), object())  # type: ignore[arg-type]

    assert checkpoints == []


@pytest.mark.parametrize("event", [msg.ToolCompleted(), msg.ToolError(error="boom")])
async def test_a_tool_boundary_takes_one_checkpoint_of_the_team(event) -> None:
    checkpoints: list[Any] = []
    dispatcher, session = _dispatcher(checkpoints)
    team = object()

    await dispatcher.handle(event, team)  # type: ignore[arg-type]

    assert checkpoints == [team]
    assert session.context_budget.last_input_tokens == 0


@pytest.mark.parametrize("event", [msg.Info(text="hello"), msg.ModelCompleted(input_tokens=9)])
async def test_other_events_do_nothing(event) -> None:
    checkpoints: list[Any] = []
    dispatcher, session = _dispatcher(checkpoints)

    await dispatcher.handle(event, object())  # type: ignore[arg-type]

    assert checkpoints == []
    assert session.context_budget.last_input_tokens == 0


async def test_a_checkpoint_that_cannot_be_saved_does_not_break_the_stream() -> None:
    """The dispatcher over the real checkpointer: a failed save is swallowed there."""
    team = MagicMock()
    team.cached_session = object()
    team.asave_session = AsyncMock(side_effect=RuntimeError("disk full"))
    dispatcher = StreamEventDispatcher(
        _Session(),  # type: ignore[arg-type]
        lambda t: SessionCheckpointer(t).snapshot(),
    )

    await dispatcher.handle(msg.ToolCompleted(), team)

    team.asave_session.assert_awaited_once_with(team.cached_session)
