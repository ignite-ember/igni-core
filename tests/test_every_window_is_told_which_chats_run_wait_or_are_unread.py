"""Every window is told which chats are running, waiting or unread, and when each last moved.

:class:`SessionStateWatch` follows ``app.activity``: a chat that goes from
idle to running or waiting has had a message go in; one that goes back to
idle has finished a run. Both are recorded, and every window hears
``session_states_changed``. ``list_session_states``
answers the same shape on demand; ``mark_session_seen`` reads a chat and
tells every window.

What we pin:

* running → idle and waiting → idle record a finished run; idle → idle,
  running → waiting and a chat first seen idle record none;
* a message going in (idle, or never seen, → busy) records the chat's last
  message; an approval answered (waiting → running) is not a new message;
* the push lists the chats that are running, waiting or unread — an
  unread chat that is not loaded too — and no idle, read chat;
* a move that changes no chat (a busy lease) pushes nothing again;
* ``mark_session_seen`` reads the chat, pushes with ``cause: seen`` and
  answers the states afterwards;
* the RPCs answer through the pool, refuse an empty session id, and say
  why when nothing is mounted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from igni_core.backend.activity import Activity, ActivityState, SessionActivity
from igni_core.backend.pool_rpcs import POOL_RPCS, SessionStatesRpc
from igni_core.backend.pool_rpcs.base import UnavailableRpc
from igni_core.backend.schemas_session_states import SessionState, SessionStates
from igni_core.backend.session_state_watch import SessionStateWatch
from igni_core.core.session.session_reads import SessionReadStore
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod
from igni_core.protocol.schemas.enums import PushChannel
from igni_core.protocol.schemas.rpc import RpcErrorCode

RUNNING = ActivityState.RUNNING
WAITING = ActivityState.WAITING
IDLE = ActivityState.IDLE


def activity(**states: ActivityState) -> Activity:
    return Activity.of(
        [
            SessionActivity(
                session_id=sid, state=state, waiting_approvals=1 if state is WAITING else 0
            )
            for sid, state in states.items()
        ]
    )


class Clock:
    def __init__(self) -> None:
        self.now = 1_000

    def __call__(self) -> int:
        self.now += 1
        return self.now


class Rig:
    """A watch over a real store, a backend whose activity a test sets, and the windows."""

    def __init__(self, tmp_path: Path) -> None:
        self.reads = SessionReadStore(tmp_path / "igni.db", clock=Clock())
        self.now = activity()
        self.pushed: list[msg.Message] = []
        self.dirs: list[str] = []
        self.watch = SessionStateWatch(
            reads=self.reads,
            activity=lambda: self.now,
            project_dir_of=self._dir,
            broadcast=self.pushed.append,
        )

    def _dir(self, session_id: str) -> Path:
        self.dirs.append(session_id)
        return Path("/work") / session_id

    def moves(self, **states: ActivityState) -> None:
        self.now = activity(**states)
        self.watch.activity_moved(self.now)

    def pushes(self) -> list[dict[str, Any]]:
        assert all(isinstance(m, msg.PushNotification) for m in self.pushed)
        assert {m.channel for m in self.pushed} <= {PushChannel.SESSION_STATES_CHANGED.value}
        return [m.payload for m in self.pushed]

    def last(self) -> dict[str, dict[str, Any]]:
        return {row["session_id"]: row for row in self.pushes()[-1]["sessions"]}


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_a_run_that_ends_makes_its_chat_unread(rig: Rig) -> None:
    rig.moves(s1=RUNNING)
    rig.moves(s1=IDLE)

    assert rig.reads.unread() == {"s1"}
    # The message went in at 1001; the reply came out at 1002.
    assert rig.last() == {
        "s1": {
            "session_id": "s1",
            "state": "idle",
            "waiting_approvals": 0,
            "unread": True,
            "last_message_at": 1002,
        }
    }


def test_a_run_answered_from_a_wait_and_ended_is_unread(rig: Rig) -> None:
    rig.moves(s1=WAITING)
    rig.moves(s1=IDLE)

    assert rig.reads.unread() == {"s1"}


@pytest.mark.parametrize(
    ("before", "after"),
    [(IDLE, IDLE), (RUNNING, WAITING), (WAITING, RUNNING), (IDLE, RUNNING)],
)
def test_a_chat_that_did_not_finish_is_not_unread(
    rig: Rig, before: ActivityState, after: ActivityState
) -> None:
    rig.moves(s1=before)
    rig.moves(s1=after)

    assert rig.reads.unread() == set()


def test_a_chat_first_seen_idle_finished_nothing(rig: Rig) -> None:
    rig.moves(s1=IDLE)

    assert rig.reads.unread() == set()
    assert rig.pushes() == [{"sessions": [], "cause": "activity"}]


def test_each_mark_is_recorded_in_the_chats_own_folder(rig: Rig) -> None:
    rig.moves(s1=RUNNING, s2=RUNNING)
    rig.moves(s1=IDLE, s2=RUNNING)

    assert rig.dirs == ["s1", "s2", "s1"]


def test_a_message_going_in_moves_the_chat_at_once(rig: Rig) -> None:
    rig.moves(s1=IDLE)
    rig.moves(s1=RUNNING)

    assert rig.last()["s1"]["last_message_at"] == 1001
    assert rig.reads.unread() == set()


def test_an_approval_answered_is_not_a_new_message(rig: Rig) -> None:
    rig.moves(s1=RUNNING)
    rig.moves(s1=WAITING)
    rig.moves(s1=RUNNING)

    assert rig.last()["s1"]["last_message_at"] == 1001


def test_the_push_lists_running_waiting_and_unread_chats_only(rig: Rig) -> None:
    rig.moves(done=RUNNING)
    rig.moves(done=IDLE, busy=RUNNING, asks=WAITING, quiet=IDLE)

    assert rig.last() == {
        "busy": {
            "session_id": "busy",
            "state": "running",
            "waiting_approvals": 0,
            "unread": False,
            "last_message_at": 1003,
        },
        "asks": {
            "session_id": "asks",
            "state": "waiting",
            "waiting_approvals": 1,
            "unread": False,
            "last_message_at": 1004,
        },
        "done": {
            "session_id": "done",
            "state": "idle",
            "waiting_approvals": 0,
            "unread": True,
            "last_message_at": 1002,
        },
    }


def test_an_unread_chat_that_is_not_loaded_is_listed(rig: Rig) -> None:
    rig.reads.finished("elsewhere", "/work/elsewhere")

    rig.moves(s1=RUNNING)

    assert rig.last()["elsewhere"] == {
        "session_id": "elsewhere",
        "state": "idle",
        "waiting_approvals": 0,
        "unread": True,
        "last_message_at": 1001,
    }


def test_a_move_that_changes_no_chat_pushes_nothing_again(rig: Rig) -> None:
    rig.moves(s1=RUNNING)
    rig.moves(s1=RUNNING)
    rig.moves(s1=RUNNING)

    assert len(rig.pushes()) == 1


def test_marking_a_chat_seen_reads_it_and_tells_every_window(rig: Rig) -> None:
    rig.moves(s1=RUNNING)
    rig.moves(s1=IDLE)

    answered = rig.watch.mark_seen("s1")

    assert rig.reads.unread() == set()
    assert answered == SessionStates(sessions=[], cause="seen")
    assert rig.pushes()[-1] == {"sessions": [], "cause": "seen"}


def test_a_chat_seen_while_it_runs_stays_listed_as_running(rig: Rig) -> None:
    rig.moves(s1=RUNNING)

    answered = rig.watch.mark_seen("s1")

    assert answered.sessions == [SessionState(session_id="s1", state=RUNNING, last_message_at=1001)]


def test_after_a_look_the_same_states_are_pushed_again_on_a_move(rig: Rig) -> None:
    """A look is a push of its own; the next real move is still told."""
    rig.moves(s1=RUNNING)
    rig.watch.mark_seen("s1")
    rig.moves(s1=IDLE)

    assert [p["cause"] for p in rig.pushes()] == ["activity", "seen", "activity"]
    assert rig.last()["s1"]["unread"] is True


def test_the_states_on_demand_are_what_a_push_would_say(rig: Rig) -> None:
    rig.moves(s1=RUNNING)

    assert rig.watch.states().model_dump(mode="json") == rig.pushes()[-1]


async def test_list_session_states_answers_through_the_pool(rig: Rig) -> None:
    rig.moves(s1=RUNNING)
    rpc = SessionStatesRpc(rig.watch)

    answer = await rpc.answer(msg.RPCRequest(id="1", method="list_session_states", args={}))

    assert answer.error == ""
    assert answer.wire_result() == rig.watch.states().model_dump()


async def test_mark_session_seen_answers_through_the_pool(rig: Rig) -> None:
    rig.moves(s1=RUNNING)
    rig.moves(s1=IDLE)
    rpc = SessionStatesRpc(rig.watch)

    answer = await rpc.answer(
        msg.RPCRequest(id="1", method="mark_session_seen", args={"session_id": "s1"})
    )

    assert answer.error == ""
    assert answer.wire_result() == {"sessions": [], "cause": "seen"}
    assert rig.reads.unread() == set()


@pytest.mark.parametrize("args", [{}, {"session_id": ""}, {"session_id": "s1", "extra": 1}])
async def test_mark_session_seen_refuses_arguments_that_do_not_fit(
    rig: Rig, args: dict[str, Any]
) -> None:
    rpc = SessionStatesRpc(rig.watch)

    answer = await rpc.answer(msg.RPCRequest(id="1", method="mark_session_seen", args=args))

    assert answer.code == RpcErrorCode.INVALID
    assert rig.pushes() == []


@pytest.mark.parametrize("method", [RpcMethod.LIST_SESSION_STATES, RpcMethod.MARK_SESSION_SEEN])
async def test_with_nothing_mounted_each_says_why(method: RpcMethod) -> None:
    [placeholder] = [p for p in SessionStatesRpc.unmounted() if method in p.methods]

    answer = await placeholder.answer(msg.RPCRequest(id="1", method=method.value, args={}))

    assert isinstance(placeholder, UnavailableRpc)
    assert answer.code == RpcErrorCode.UNAVAILABLE
    assert answer.error == SessionStatesRpc.NOT_WATCHED


def test_the_pool_knows_both_methods() -> None:
    assert SessionStatesRpc in POOL_RPCS
    assert SessionStatesRpc.methods == {
        RpcMethod.LIST_SESSION_STATES,
        RpcMethod.MARK_SESSION_SEEN,
    }
