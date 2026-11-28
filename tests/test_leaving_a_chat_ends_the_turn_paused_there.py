"""Leaving a chat with ``/clear`` or ``/fork`` ends the turn paused there.

``/clear`` while a turn waited on an approval carried the approval into the
new chat: a window attaching there was offered it, and answering it resumed
the old chat's run beside the new one, so the session never went idle. A
run resumed before ``/clear`` or ``/fork`` that finished afterwards was
announced in the new chat and saved in the old one.

What happens instead: leaving a chat ends a paused turn there exactly as a
new message does — its approvals go and every window withdraws them, the
turn stays in that chat's history with its waiting calls declined — and
nothing of the old chat appears in the new one. A turn still working when
the chat is left finishes in its own chat, and is told there.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from agno.run.base import RunStatus

from igni_core.backend.activity import ActivityState
from igni_core.core.config.models import ModelRegistry
from igni_core.protocol import messages as msg
from tests.test_a_message_sent_during_a_turn_is_read_after_it import (
    DECLINED,
    QUESTION,
    Brain,
    Rig,
    ScriptedModel,
)

NEXT = "Something else entirely"


@pytest.fixture
def brain(monkeypatch) -> Brain:
    brain = Brain()
    monkeypatch.setattr(ModelRegistry, "get_model", lambda self, name=None: ScriptedModel(brain))
    monkeypatch.setattr(ModelRegistry, "get_context_window", lambda self, name=None: 200_000)
    return brain


@pytest.fixture
async def rig(tmp_path: Path, brain: Brain):
    rig = await Rig(tmp_path).start()
    try:
        yield rig
    finally:
        await rig.settle()
        await rig.stop()


async def _runs_of(rig: Rig, session_id: str) -> list[Any]:
    found = await rig.backend._session.db.get_runs(  # noqa: SLF001 — the stored history
        session_id=session_id, deserialize=True
    )
    runs = found[0] if isinstance(found, tuple) else found
    return [r for r in runs or [] if r.parent_run_id is None]


async def _paused(rig: Rig) -> str:
    await rig.send(msg.UserMessage(id="m-1", text=QUESTION))
    (requirement_id,) = rig.waiting()
    return requirement_id


async def _leave(rig: Rig, command: str) -> str:
    """Type ``command``; the chat the session is in afterwards."""
    await asyncio.wait_for(rig.send(msg.Command(id=f"c-{command}", text=command)), 30)
    return rig.backend.session_id


def _withdrawn(rig: Rig) -> list[tuple[str, str]]:
    return [(m.requirement_id, m.session_id) for m in rig.windows.of(msg.RequirementResolved)]


async def _closed_declined(rig: Rig, session_id: str) -> None:
    (turn,) = await _runs_of(rig, session_id)
    assert turn.status == RunStatus.completed, "the paused turn is ended, not left paused"
    (declined,) = [m for m in turn.messages if m.role == "tool"]
    assert declined.content == DECLINED and declined.tool_call_id == "shell-1"


async def _answered_alone(rig: Rig, brain: Brain, chat: str) -> None:
    """A new message in ``chat`` is answered, shown nothing of the chat left."""
    calls = brain.calls
    await asyncio.wait_for(rig.send(msg.UserMessage(id="m-next", text=NEXT)), 30)
    await rig.settle()
    assert brain.calls == calls + 1, "the new message is answered"
    shown = brain.shown[-1]
    assert shown.has(NEXT) and not shown.has(QUESTION), shown.lines
    assert [r.status for r in await _runs_of(rig, chat)] == [RunStatus.completed]
    assert rig.backend.activity().state is ActivityState.IDLE


# ── /clear while a turn waits ────────────────────────────────────


async def test_clear_ends_the_paused_turn_in_its_own_chat(rig: Rig, brain: Brain) -> None:
    requirement_id = await _paused(rig)
    old = rig.backend.session_id

    new = await _leave(rig, "/clear")

    assert new != old
    assert rig.waiting() == [], "nothing waits in the new chat"
    assert _withdrawn(rig) == [(requirement_id, old)], "every window of the old chat withdraws it"
    await _closed_declined(rig, old)
    assert not (rig.project / "deployed.txt").exists()
    assert await rig.backend.get_pending_messages(old) == [], "the old chat's message is done"
    assert await _runs_of(rig, new) == []
    await _answered_alone(rig, brain, new)


async def test_answering_the_old_approval_after_clear_runs_nothing(rig: Rig, brain: Brain) -> None:
    requirement_id = await _paused(rig)
    await _leave(rig, "/clear")
    calls = brain.calls

    answer = msg.HITLResponse(id="h-1", requirement_id=requirement_id, action="confirm")
    await asyncio.wait_for(rig.send(answer), 30)
    await rig.settle()

    assert rig.errors("h-1") == [f"Unknown requirement: {requirement_id}"]
    assert not (rig.project / "deployed.txt").exists()
    assert brain.calls == calls, "nothing resumes the old chat's run"
    assert rig.backend.activity().state is ActivityState.IDLE
    assert not rig.backend.processing
    await _answered_alone(rig, brain, rig.backend.session_id)


async def test_a_window_attaching_to_the_new_chat_is_offered_nothing(
    rig: Rig, brain: Brain
) -> None:
    await _paused(rig)
    new = await _leave(rig, "/clear")

    attach = msg.RPCRequest(id="a-1", method="attach_session", args={"session_id": new})
    await asyncio.wait_for(rig.send(attach), 30)

    replayed = [m for m in rig.windows.of(msg.RunPaused) if m.id == "a-1"]
    assert replayed == [], "no approval of the old chat shows in the new one"


async def test_clear_ends_a_turn_a_restart_left_paused(tmp_path: Path, brain: Brain) -> None:
    before = await Rig(tmp_path).start()
    try:
        await _paused(before)
        old = before.backend.session_id
    finally:
        await before.settle()
        await before.stop()

    after = await Rig(tmp_path, resume=old).start()
    try:
        (requirement_id,) = after.waiting()
        new = await _leave(after, "/clear")

        assert after.waiting() == []
        assert _withdrawn(after) == [(requirement_id, old)]
        await _closed_declined(after, old)
        await _answered_alone(after, brain, new)
    finally:
        await after.settle()
        await after.stop()


# ── /fork while a turn waits ─────────────────────────────────────


async def test_fork_ends_the_paused_turn_and_carries_it_ended(rig: Rig, brain: Brain) -> None:
    requirement_id = await _paused(rig)
    old = rig.backend.session_id

    new = await _leave(rig, "/fork")

    assert new != old
    assert rig.waiting() == []
    assert _withdrawn(rig) == [(requirement_id, old)]
    await _closed_declined(rig, old)
    await _closed_declined(rig, new)

    await asyncio.wait_for(rig.send(msg.UserMessage(id="m-next", text=NEXT)), 30)
    await rig.settle()
    shown = brain.shown[-1]
    assert 0 <= shown.position(QUESTION) < shown.position(DECLINED) < shown.position(NEXT)
    assert rig.backend.activity().state is ActivityState.IDLE


# ── A resumed turn still working when the chat is left ───────────


@pytest.mark.parametrize("command", ["/clear", "/fork"])
async def test_a_resumed_turn_finishing_after_the_chat_is_left_is_told_where_it_is_saved(
    rig: Rig, brain: Brain, command: str
) -> None:
    requirement_id = await _paused(rig)
    old = rig.backend.session_id
    brain.after_shell = "read"
    brain.hold()
    resumed = rig.send(msg.HITLResponse(id="h-1", requirement_id=requirement_id, action="confirm"))
    await asyncio.wait_for(brain.holding.wait(), 10)

    new = await _leave(rig, command)
    brain.release.set()
    await asyncio.wait_for(resumed, 30)
    await rig.settle()

    assert new != old
    (turn,) = await _runs_of(rig, old)
    assert turn.status == RunStatus.completed, "saved in the chat it ran in"
    told = {m.session_id for m in rig.windows.of(msg.RunCompleted) if m.run_id == turn.run_id}
    assert told == {old}, "announced in the chat it is saved in, never the new one"
    assert (rig.project / "deployed.txt").exists(), "the approved call ran"
    assert rig.backend.activity().state is ActivityState.IDLE
