"""A chat whose run finishes is unread in every window until someone looks.

End to end over the real pool and host model: a person's message starts
a run, the run's messages move ``app.activity``, and every window hears
``session_states_changed`` — running while it works, unread once it is
done. A window that calls ``mark_session_seen`` reads it for all of them,
and ``list_session_states`` says the same as the pushes.

What we pin:

* while the run works, every window is told the chat is running;
* when it ends, every window is told the chat is unread, and the global
  store has recorded it;
* ``list_session_states`` over the window's RPC answers the same;
* ``mark_session_seen`` over the window's RPC reads it, and every
  window is told;
* a backend built without a read store answers both methods ``unavailable``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from igni_core.backend.activity import ActivityState, SessionActivity
from igni_core.backend.pool_rpcs import SessionStatesRpc
from igni_core.core.session.session_reads import SessionReadStore
from igni_core.protocol import messages as msg
from tests.host_model_helpers import HostRig, Sink


def _follow_processing(rig: HostRig) -> None:
    """The fake session reports itself running while its run is in progress."""
    session = rig.session

    def activity() -> SessionActivity:
        state = ActivityState.RUNNING if session.processing else ActivityState.IDLE
        return SessionActivity(session_id=session.session_id, state=state)

    session.activity = activity  # type: ignore[method-assign]


@pytest.fixture
def reads(tmp_path: Path) -> SessionReadStore:
    return SessionReadStore(tmp_path / "global.db")


@pytest.fixture
async def rig(tmp_path: Path, reads: SessionReadStore) -> AsyncIterator[HostRig]:
    built = HostRig(tmp_path)
    _follow_processing(built)
    built.session.run_release = asyncio.Event()
    await built.start(reads=reads)
    yield built
    await built.stop()


def _states(rig: HostRig) -> list[dict[str, Any]]:
    return [p.payload for p in rig.windows.pushes("session_states_changed")]


async def _told(rig: HostRig, state: str) -> list[dict[str, Any]]:
    """The last push, once it says ``boot`` is in ``state``; a few seconds at most."""
    async with asyncio.timeout(5):
        while True:
            pushed = _states(rig)
            if pushed and any(r["state"] == state for r in pushed[-1]["sessions"]):
                return pushed[-1]["sessions"]
            await asyncio.sleep(0.01)


async def _rpc(rig: HostRig, method: str, **args: Any) -> Any:
    assert rig.orchestrator is not None
    request_id = f"{method}-{len(rig.windows.sent)}"
    await rig.orchestrator.dispatch(msg.RPCRequest(id=request_id, method=method, args=args))
    await rig.settle()
    [answer] = [m for m in rig.windows.of(msg.RPCResponse) if m.id == request_id]
    return answer


async def test_a_run_shows_running_then_unread_then_read(
    rig: HostRig, reads: SessionReadStore
) -> None:
    assert rig.orchestrator is not None
    assert rig.session.run_release is not None
    run = rig.orchestrator.spawn_dispatch(msg.UserMessage(text="Work.", client_id="ws-1"))

    assert await _told(rig, "running") == [
        {"session_id": "boot", "state": "running", "waiting_approvals": 0, "unread": False}
    ]

    rig.session.run_release.set()
    async with asyncio.timeout(5):
        await run

    assert await _told(rig, "idle") == [
        {"session_id": "boot", "state": "idle", "waiting_approvals": 0, "unread": True}
    ]
    assert reads.unread() == {"boot"}

    listed = await _rpc(rig, "list_session_states")
    assert listed.error is None
    assert listed.result["sessions"][0]["unread"] is True

    seen = await _rpc(rig, "mark_session_seen", session_id="boot")
    assert seen.error is None
    assert seen.result == {"sessions": [], "cause": "seen"}
    assert _states(rig)[-1] == {"sessions": [], "cause": "seen"}
    assert reads.unread() == set()


async def test_without_a_read_store_both_methods_say_unavailable(tmp_path: Path) -> None:
    rig = HostRig(tmp_path)
    await rig.start(with_reads=False)
    try:
        for method, args in (
            ("list_session_states", {}),
            ("mark_session_seen", {"session_id": "boot"}),
        ):
            answer = await _rpc(rig, method, **args)
            assert answer.result is None
            assert SessionStatesRpc.NOT_WATCHED in (answer.error or "")
    finally:
        await rig.stop()


def _watching(rig: HostRig, extension_id: str) -> Sink:
    sink = Sink()
    assert rig.model is not None
    bus = rig.model.services.bus
    bus.subscribe(bus.attach(extension_id, sink), ["sessions.states_changed"])
    return sink


async def test_an_extension_reads_the_states_marks_seen_and_hears_each_move(
    rig: HostRig, reads: SessionReadStore
) -> None:
    ext = rig.install("acme.board", host=["sessions"])
    sink = _watching(rig, ext)
    reads.finished("boot", rig.project)

    states = await rig.ask(ext, "get", "sessions.states")
    assert states.ok, states.error
    assert states.result == [
        {"session_id": "boot", "state": "idle", "waiting_approvals": 0, "unread": True}
    ]

    seen = await rig.ask(ext, "call", "sessions.mark_seen", session_id="boot")
    assert seen.ok, seen.error
    assert seen.result == {"sessions": [], "cause": "seen"}
    await rig.settle()

    assert reads.unread() == set()
    assert sink.values("sessions.states_changed")[-1] == {"sessions": [], "cause": "seen"}


async def test_an_extension_without_the_sessions_grant_sees_neither(rig: HostRig) -> None:
    ext = rig.install("acme.board", host=["chat.read"])

    for op, member, params in (
        ("get", "sessions.states", {}),
        ("call", "sessions.mark_seen", {"session_id": "boot"}),
    ):
        reply = await rig.ask(ext, op, member, **params)
        assert not reply.ok
        assert reply.error.code == "not_granted"


async def test_marking_no_chat_seen_is_refused(rig: HostRig) -> None:
    ext = rig.install("acme.board", host=["sessions"])

    reply = await rig.ask(ext, "call", "sessions.mark_seen", session_id="")

    assert not reply.ok
    assert reply.error.code == "invalid"


async def test_without_a_read_store_the_members_say_unavailable(tmp_path: Path) -> None:
    rig = HostRig(tmp_path)
    await rig.start(with_reads=False)
    try:
        ext = rig.install("acme.board", host=["sessions"])
        for op, member, params in (
            ("get", "sessions.states", {}),
            ("call", "sessions.mark_seen", {"session_id": "boot"}),
        ):
            reply = await rig.ask(ext, op, member, **params)
            assert not reply.ok
            assert reply.error.code == "unavailable"
    finally:
        await rig.stop()
