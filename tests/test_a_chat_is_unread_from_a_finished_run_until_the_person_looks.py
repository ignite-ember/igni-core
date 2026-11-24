"""A chat is unread from a finished run until the person looks at it.

The sidebar marks a chat whose run finished while the person was
elsewhere. The marks come from :class:`SessionReadStore` in the global
database — what the person has seen is theirs, across windows, devices
and restarts — and ride on ``list_sessions`` as ``unread``.

What we pin:

* a finished run makes its chat unread; looking at it reads it;
* a run that finishes after the look makes the chat unread again;
* a look and a finish in the same millisecond leave the chat read;
* a look with no finished run, or a chat never recorded, is not unread;
* the marks survive a new store over the same database (a restart);
* a store that cannot be read answers nothing unread, and a failed write
  raises nothing;
* an empty session id is not recorded;
* ``list_sessions`` marks exactly the unread chats, and marks none when
  nothing tells it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.backend.server_sessions import SessionsController
from igni_core.core.db.database import Database
from igni_core.core.session.schemas import SessionListRow
from igni_core.core.session.session_reads import SessionReadStore


class Clock:
    """Milliseconds a test sets."""

    def __init__(self, now: int = 1_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "igni.db")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def reads(db: Database, clock: Clock) -> SessionReadStore:
    return SessionReadStore(db, clock=clock)


PROJECT = "/work/igni-app"


def test_a_finished_run_makes_its_chat_unread(reads: SessionReadStore) -> None:
    reads.finished("s1", PROJECT)

    assert reads.unread() == {"s1"}


def test_looking_at_the_chat_reads_it(reads: SessionReadStore, clock: Clock) -> None:
    reads.finished("s1", PROJECT)
    clock.now += 5

    reads.seen("s1", PROJECT)

    assert reads.unread() == set()


def test_a_run_that_finishes_after_the_look_makes_it_unread_again(
    reads: SessionReadStore, clock: Clock
) -> None:
    reads.finished("s1", PROJECT)
    clock.now += 5
    reads.seen("s1", PROJECT)
    clock.now += 5

    reads.finished("s1", PROJECT)

    assert reads.unread() == {"s1"}


def test_a_look_in_the_same_millisecond_as_the_finish_reads_it(
    reads: SessionReadStore,
) -> None:
    reads.finished("s1", PROJECT)
    reads.seen("s1", PROJECT)

    assert reads.unread() == set()


def test_a_look_with_no_finished_run_is_not_unread(reads: SessionReadStore) -> None:
    reads.seen("s1", PROJECT)

    assert reads.unread() == set()


def test_only_the_chats_with_news_are_unread(reads: SessionReadStore, clock: Clock) -> None:
    reads.finished("s1", PROJECT)
    reads.finished("s2", PROJECT)
    clock.now += 1
    reads.seen("s2", PROJECT)

    assert reads.unread() == {"s1"}


def test_the_marks_survive_a_restart(db: Database, reads: SessionReadStore) -> None:
    reads.finished("s1", PROJECT)

    assert SessionReadStore(db).unread() == {"s1"}


def test_the_store_times_in_milliseconds_by_default(tmp_path: Path) -> None:
    """Two marks a millisecond apart are told apart without a clock handed in."""
    store = SessionReadStore(tmp_path / "igni.db")
    store.finished("s1", PROJECT)

    assert store.unread() == {"s1"}


def test_a_store_that_cannot_be_read_answers_nothing_unread(tmp_path: Path) -> None:
    broken = SessionReadStore(tmp_path / "igni.db")
    broken._db = MagicMock()
    broken._db.sync_session.side_effect = OSError("disk gone")

    assert broken.unread() == set()


def test_a_failed_write_raises_nothing(tmp_path: Path) -> None:
    broken = SessionReadStore(tmp_path / "igni.db")
    broken._db = MagicMock()
    broken._db.sync_session.side_effect = OSError("disk gone")

    broken.finished("s1", PROJECT)
    broken.seen("s1", PROJECT)


def test_an_empty_session_id_is_not_recorded(reads: SessionReadStore) -> None:
    reads.finished("", PROJECT)

    assert reads.unread() == set()


def _controller(unread: SessionReadStore | None, *session_ids: str) -> SessionsController:
    session = MagicMock()
    session.persistence.list_sessions = AsyncMock(
        return_value=[SessionListRow(session_id=sid, name=sid) for sid in session_ids]
    )
    return SessionsController(
        session=session,
        chat_history_provider=AsyncMock(return_value=[]),
        unread_sessions=unread.unread if unread is not None else None,
    )


async def test_the_session_list_marks_the_unread_chats(reads: SessionReadStore) -> None:
    reads.finished("s2", PROJECT)

    listed = await _controller(reads, "s1", "s2", "s3").list_sessions()

    assert {e.session_id: e.unread for e in listed.sessions} == {
        "s1": False,
        "s2": True,
        "s3": False,
    }


async def test_with_nothing_to_tell_it_no_chat_is_unread() -> None:
    listed = await _controller(None, "s1").list_sessions()

    assert [e.unread for e in listed.sessions] == [False]
