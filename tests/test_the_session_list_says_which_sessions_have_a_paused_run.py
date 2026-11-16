"""The session list says which sessions have a run waiting for an answer.

A run that paused on an approval leaves the person with a question. The
sidebar has to say which session is waiting, or the person cannot find
the run to answer (campaign R647): the session list carried no such
sign, so a paused session looked like any other.

The answer is read from the approval journal — the project's record of
what waits on the person — rather than from the running session's
memory, so a session nobody has loaded still says it is waiting and a
backend that restarted still says so.

What we pin:

* the session with an approval waiting is marked, the others are not;
* a session with nothing waiting is not marked;
* the mark survives a restart, because it is read from the journal;
* answering the approval clears the mark;
* a requirement the permission policy decided by itself is not a person
  waiting — the session is not marked;
* a sub-agent's approval marks the session as well;
* the field is optional, so an older client reading the list is not
  troubled by it;
* a journal that cannot be read leaves the list as it was.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from agno.models.response import ToolExecution
from agno.run.requirement import RunRequirement

from igni_core.backend.server_sessions import SessionsController
from igni_core.core.approvals.journal import ApprovalJournal
from igni_core.core.approvals.records import ApprovalOrigin
from igni_core.core.db.database import Database
from igni_core.core.session.schemas import SessionListRow
from igni_core.protocol import messages as msg


def _requirement(call_id: str = "call-1") -> RunRequirement:
    return RunRequirement(
        tool_execution=ToolExecution(
            tool_call_id=call_id,
            tool_name="run_shell_command",
            tool_args={"command": "echo hello"},
            requires_confirmation=True,
        )
    )


def _request(call_id: str = "call-1") -> msg.HITLRequest:
    return msg.HITLRequest(requirement_id=call_id, tool_name="run_shell_command")


def _rows(*session_ids: str) -> list[SessionListRow]:
    return [
        SessionListRow(session_id=session_id, name=f"Session {session_id}", run_count=1)
        for session_id in session_ids
    ]


def _controller(journal: ApprovalJournal, *session_ids: str) -> SessionsController:
    """A sessions controller over a session whose project holds ``session_ids``."""
    session = MagicMock()
    session.persistence.list_sessions = AsyncMock(return_value=_rows(*session_ids))
    return SessionsController(
        session=session,
        chat_history_provider=AsyncMock(return_value=[]),
        waiting_sessions=journal.waiting_sessions,
    )


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "approvals.db")


@pytest.fixture
def journal(db: Database) -> ApprovalJournal:
    return ApprovalJournal(db)


def _record(
    journal: ApprovalJournal,
    session_id: str,
    requirement_id: str = "req-1",
    origin: ApprovalOrigin = ApprovalOrigin.MAIN,
) -> None:
    journal.record(
        requirement_id=requirement_id,
        session_id=session_id,
        run_id="run-1",
        origin=origin,
        requirement=_requirement(requirement_id),
        request=_request(requirement_id),
    )


async def test_the_session_that_waits_is_marked(journal: ApprovalJournal) -> None:
    """The one with an approval waiting; the others are not."""
    _record(journal, "s2")
    controller = _controller(journal, "s1", "s2", "s3")

    listed = await controller.list_sessions()

    assert {entry.session_id: entry.has_paused_run for entry in listed.sessions} == {
        "s1": False,
        "s2": True,
        "s3": False,
    }


async def test_a_session_with_nothing_waiting_is_not_marked(journal: ApprovalJournal) -> None:
    """Nothing waits: nothing is marked."""
    controller = _controller(journal, "s1", "s2")

    listed = await controller.list_sessions()

    assert [entry.has_paused_run for entry in listed.sessions] == [False, False]


async def test_the_mark_survives_a_restart(db: Database, tmp_path: Path) -> None:
    """The mark is read from the journal, so a backend that starts later says so.

    The approval was raised before the restart; a backend that reads its
    own memory would say no session is waiting, and the person would
    never find the run to answer.
    """
    _record(ApprovalJournal(db), "s2")
    restarted = ApprovalJournal(Database(tmp_path / "approvals.db"))

    listed = await _controller(restarted, "s1", "s2").list_sessions()

    assert {entry.session_id: entry.has_paused_run for entry in listed.sessions} == {
        "s1": False,
        "s2": True,
    }


async def test_answering_the_approval_clears_the_mark(journal: ApprovalJournal) -> None:
    """The person answered: the session is not waiting any more."""
    _record(journal, "s1")
    controller = _controller(journal, "s1")
    assert (await controller.list_sessions()).sessions[0].has_paused_run is True

    journal.forget("req-1")

    assert (await controller.list_sessions()).sessions[0].has_paused_run is False


async def test_a_requirement_the_policy_decided_is_not_a_person_waiting(
    journal: ApprovalJournal,
) -> None:
    """The evaluator's own decision is kept for the run's resume, not for a person."""
    journal.record_auto_resolved(
        session_id="s1", run_id="run-1", requirement=_requirement("auto-1")
    )
    controller = _controller(journal, "s1")

    listed = await controller.list_sessions()

    assert listed.sessions[0].has_paused_run is False


async def test_a_sub_agents_approval_marks_the_session(journal: ApprovalJournal) -> None:
    """A sub-agent's question is a question the person has to answer."""
    _record(journal, "s1", "req-sub", ApprovalOrigin.SUB_AGENT)
    controller = _controller(journal, "s1")

    listed = await controller.list_sessions()

    assert listed.sessions[0].has_paused_run is True


async def test_the_field_is_optional_for_an_older_client(journal: ApprovalJournal) -> None:
    """The mark is a new field: a client that does not know it reads the list.

    The entry is permissive (``extra='allow'``), so the field travels
    without a schema-drift error, and an entry without it is still an
    entry.
    """
    entry = msg.SessionListEntry(session_id="s1", name="One")

    assert entry.has_paused_run is False
    assert (
        msg.SessionListEntry.model_validate({**entry.model_dump(), "unknown": 1}).session_id == "s1"
    )


async def test_a_journal_that_cannot_be_read_leaves_the_list_as_it_was() -> None:
    """A broken database does not take the list down with it."""
    broken = MagicMock()
    broken.waiting_sessions.side_effect = RuntimeError("the database is gone")
    session = MagicMock()
    session.persistence.list_sessions = AsyncMock(return_value=_rows("s1", "s2"))
    controller = SessionsController(
        session=session,
        chat_history_provider=AsyncMock(return_value=[]),
        waiting_sessions=broken.waiting_sessions,
    )

    listed = await controller.list_sessions()

    assert [entry.session_id for entry in listed.sessions] == ["s1", "s2"]
    assert [entry.has_paused_run for entry in listed.sessions] == [False, False]
