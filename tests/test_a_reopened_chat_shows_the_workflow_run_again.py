"""A reopened chat shows the workflow run again.

A workflow started from the composer is an RPC that returns at once:
the run goes on in the background, and its card is drawn from the
``workflow_event`` pushes. A chat reopened after that had no card at
all — the pushes are gone, and nothing in the history said the run
existed, so the reopened chat was empty (campaign R664).

The run's tape is kept in the session's event log. This pins that the
rebuilt history carries it, and where the card lands.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from igni_core.backend.schemas_history import (
    AssistantTurn,
    ChatTurn,
    UserTurn,
    WorkflowTurn,
)
from igni_core.backend.schemas_workflow_events import WorkflowEventRecord
from igni_core.backend.server_history import ChatHistoryRebuilder
from igni_core.core.session.event_log_schema import WorkflowEventEntry


class _Persistence:
    """The stored event logs, by session."""

    def __init__(self, stored: dict[str, list[Any]] | None = None) -> None:
        self.stored = stored or {}

    async def load_event_log_of(self, session_id: str) -> list[Any]:
        return list(self.stored.get(session_id, []))


def _session(runs: Any, events: list[Any] = (), *, current: str = "sess-1"):
    agno_session = None if runs is None else SimpleNamespace(runs=runs)
    team = SimpleNamespace(aget_session=AsyncMock(return_value=agno_session))
    return SimpleNamespace(
        main_team=team,
        user_id="u",
        session_id=current,
        plan_store=SimpleNamespace(decisions={}),
        event_log=list(events),
        persistence=_Persistence(),
    )


def _run(run_id: str, *messages: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(run_id=run_id, messages=list(messages))


def _msg(role: str, content: str = "", created_at: int = 0) -> SimpleNamespace:
    return SimpleNamespace(role=role, content=content, created_at=created_at)


def _record(
    workflow_run_id: str,
    workflow_name: str,
    type_: str,
    ts: int,
    seq: int,
    **payload: Any,
) -> WorkflowEventRecord:
    return WorkflowEventRecord(
        workflow_run_id=workflow_run_id,
        name=workflow_name,
        ts=ts,
        seq=seq,
        type=type_,
        payload=payload,
    )


def _entry(seq: int, record: WorkflowEventRecord) -> WorkflowEventEntry:
    return WorkflowEventEntry(seq=seq, run_id="", payload=record)


async def _rebuild(session: Any) -> list[ChatTurn]:
    return await ChatHistoryRebuilder(session).rebuild("sess-1")


async def test_the_run_is_in_the_reopened_chat_with_its_tape() -> None:
    """The card comes back, carrying the events the run produced."""
    events = [
        _entry(
            1,
            _record(
                "wf_1",
                "file-facts",
                "workflow_started",
                1_700_000_000_000,
                0,
                name="file-facts",
            ),
        ),
        _entry(
            2,
            _record(
                "wf_1",
                "file-facts",
                "agent_started",
                1_700_000_001_000,
                1,
                id="req_1",
                label="facts",
            ),
        ),
        _entry(
            3,
            _record(
                "wf_1",
                "file-facts",
                "workflow_completed",
                1_700_000_002_000,
                2,
                status="completed",
            ),
        ),
    ]

    turns = await _rebuild(_session([], events))

    [card] = [t for t in turns if isinstance(t, WorkflowTurn)]
    assert card.workflow_run_id == "wf_1"
    assert card.name == "file-facts"
    # The tape is the very envelopes the push carried, oldest first.
    assert [e["type"] for e in card.events] == [
        "workflow_started",
        "agent_started",
        "workflow_completed",
    ]
    assert card.events[0]["workflow_run_id"] == "wf_1"
    assert card.events[0]["ts"] == 1_700_000_000_000
    assert card.events[0]["payload"] == {"name": "file-facts"}


async def test_the_card_lands_where_the_run_happened() -> None:
    """The card sits after the turns written before the run started."""
    runs = [
        _run(
            "r1",
            _msg("user", "before", created_at=1_700_000_000),
            _msg("assistant", "answer", created_at=1_700_000_010),
        ),
    ]
    events = [
        _entry(
            1,
            _record(
                "wf_1",
                "file-facts",
                "workflow_started",
                1_700_000_100_000,
                0,
                name="file-facts",
            ),
        ),
    ]

    turns = await _rebuild(_session(runs, events))

    kinds = [type(t) for t in turns]
    assert kinds.index(WorkflowTurn) > kinds.index(AssistantTurn)
    assert turns[-1].workflow_run_id == "wf_1"  # type: ignore[attr-defined]


async def test_a_chat_with_no_workflow_run_is_left_alone() -> None:
    """Nothing to show: the history is what it was."""
    runs = [_run("r1", _msg("user", "hi", created_at=1_700_000_000))]

    turns = await _rebuild(_session(runs, []))

    assert not [t for t in turns if isinstance(t, WorkflowTurn)]
    assert [t.content for t in turns if isinstance(t, UserTurn)] == ["hi"]


async def test_two_runs_are_two_cards() -> None:
    """Each run gets its own card, in the order the runs happened."""
    events = [
        _entry(1, _record("wf_1", "one", "workflow_started", 1_700_000_100_000, 0)),
        _entry(2, _record("wf_2", "two", "workflow_started", 1_700_000_200_000, 0)),
        _entry(3, _record("wf_1", "one", "workflow_completed", 1_700_000_300_000, 1)),
    ]

    turns = await _rebuild(_session([], events))

    cards = [t for t in turns if isinstance(t, WorkflowTurn)]
    assert [c.workflow_run_id for c in cards] == ["wf_1", "wf_2"]
    # Each card holds its own run's tape, not the other's.
    assert [e["type"] for e in cards[0].events] == ["workflow_started", "workflow_completed"]
    assert [e["type"] for e in cards[1].events] == ["workflow_started"]
