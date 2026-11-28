"""A turn the person cancelled, or whose model call failed, is recapped to the next message.

Agno leaves a cancelled or errored run out of the history it shows the
model, so the next message was read as if that turn never happened. The
next message carries the interrupted-run recap a crash already gets —
the same words — once, in the chat the turn was in, after a restart too,
and not when the turn is in the history anyway.

Every test runs a real backend with the real agent and database; only the
language model is a script, so what it was shown is what it is asked.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from agno.models.message import Message
from agno.models.response import ModelResponse

from igni_core.backend.schemas_lifecycle import InterruptedRunSummary
from igni_core.backend.schemas_run import HookGateResult
from igni_core.core.config.models import ModelRegistry
from igni_core.protocol import messages as msg
from tests.test_a_message_sent_during_a_turn_is_read_after_it import Rig, ScriptedModel, Shown

QUESTION = "Summarise the release notes, hold"
NEXT = "What did you find"
LATER = "And then"
FAILURE = "the model provider is down"
FAILING = "Summarise the release notes, fail"
FINISHED = "List the open issues"
SECOND = "Check the changelog, hold"
OPENING = "Previous run was interrupted before completion."
RECAP = f"{OPENING} The user had asked: {QUESTION!r}."


class Brain:
    """The scripted model: ``hold`` keeps an answer until released, ``fail`` raises,
    ``deploy`` asks for a shell call (an approval in the default mode)."""

    def __init__(self) -> None:
        self.shown: list[Shown] = []
        self.release = asyncio.Event()
        self.release.set()
        self.holding = asyncio.Event()

    async def answer(self, messages: list[Message]) -> ModelResponse:
        if any("Title:" in str(m.content) for m in messages if m.role == "user"):
            return ModelResponse(role="assistant", content="Release notes")
        self.shown.append(Shown(messages))
        text = str(messages[-1].content)
        if "hold" in text:
            self.holding.set()
            await self.release.wait()
        if "fail" in text:
            raise RuntimeError(FAILURE)
        if messages[-1].role == "user" and "deploy" in text:
            call = {"name": "run_shell_command", "arguments": json.dumps({"command": "touch x"})}
            return ModelResponse(
                role="assistant",
                tool_calls=[{"id": "shell-1", "type": "function", "function": call}],
            )
        return ModelResponse(role="assistant", content="Noted.")

    def asked(self, text: str) -> Shown:
        """The one call whose latest message is ``text``."""
        (shown,) = [s for s in self.shown if s.lines and text in s.lines[-1]]
        return shown


@pytest.fixture
def brain(monkeypatch: pytest.MonkeyPatch) -> Brain:
    brain = Brain()
    monkeypatch.setattr(ModelRegistry, "get_model", lambda self, name=None: ScriptedModel(brain))
    monkeypatch.setattr(ModelRegistry, "get_context_window", lambda self, name=None: 200_000)
    return brain


@pytest.fixture
async def rig(tmp_path: Path, brain: Brain):
    rig = await _started(Rig(tmp_path))
    try:
        yield rig
    finally:
        await rig.settle()
        await rig.stop()


async def _started(rig: Rig) -> Rig:
    """Started as a backend is: its startup reads what the last one left."""
    await rig.start()
    await rig.backend.startup()
    return rig


async def _cancelled(rig: Rig, brain: Brain, text: str = QUESTION) -> None:
    brain.release.clear()
    brain.holding.clear()
    turn = rig.send(msg.UserMessage(id=f"m-{len(brain.shown)}", text=text))
    await asyncio.wait_for(brain.holding.wait(), 10)
    await rig.send(msg.Cancel(id="c-1"))
    brain.release.set()
    await asyncio.wait_for(turn, 30)
    await rig.settle()


async def _say(rig: Rig, text: str, request_id: str) -> None:
    await asyncio.wait_for(rig.send(msg.UserMessage(id=request_id, text=text)), 30)
    await rig.settle()


def _recapped(shown: Shown, recap: str, text: str) -> None:
    """``shown`` reads the recap, then the message it rides with, in one user turn."""
    note = f"{recap} {InterruptedRunSummary.WITH_USER_MESSAGE}"
    assert shown.lines[-1].startswith("user: "), shown.lines
    latest = shown.lines[-1]
    assert 0 <= latest.find(note) < latest.find(text), shown.lines
    assert "\n".join(shown.lines).count(OPENING) == 1, shown.lines


def _read_before(shown: Shown) -> None:
    """``shown`` has the recap once, in the turn it came with, and not with its own message."""
    assert OPENING not in shown.lines[-1], shown.lines
    assert "\n".join(shown.lines).count(OPENING) == 1, shown.lines


async def test_the_message_after_a_cancelled_turn_reads_the_recap(rig: Rig, brain: Brain) -> None:
    await _cancelled(rig, brain)

    await _say(rig, NEXT, "m-2")

    _recapped(brain.asked(NEXT), RECAP, NEXT)
    assert await rig.backend.get_interrupted_runs(rig.backend.session_id) == [], (
        "settled once the turn that read it completes"
    )


async def test_the_message_after_a_failed_turn_reads_the_recap_with_its_reason(
    rig: Rig, brain: Brain
) -> None:
    await _say(rig, FAILING, "m-1")

    await _say(rig, NEXT, "m-2")

    _recapped(
        brain.asked(NEXT), f"{OPENING} The user had asked: {FAILING!r}. Reason: {FAILURE!r}.", NEXT
    )


async def test_the_recap_is_read_once(rig: Rig, brain: Brain) -> None:
    await _cancelled(rig, brain)
    await _say(rig, NEXT, "m-2")

    await _say(rig, LATER, "m-3")

    _read_before(brain.asked(LATER))
    assert await rig.backend.get_interrupted_runs(rig.backend.session_id) == []


async def test_a_finished_turn_leaves_no_recap(rig: Rig, brain: Brain) -> None:
    await _say(rig, FINISHED, "m-1")

    await _say(rig, NEXT, "m-2")

    assert not brain.asked(NEXT).has(OPENING)


async def test_the_recap_waits_in_the_chat_the_turn_was_in(rig: Rig, brain: Brain) -> None:
    await _cancelled(rig, brain)
    chat = rig.backend.session_id

    await rig.backend.switch_session("another-chat")
    await _say(rig, "Hello there", "m-2")
    assert not brain.asked("Hello there").has(OPENING), "another chat's turn is not this one's"

    await rig.backend.switch_session(chat)
    await _say(rig, NEXT, "m-3")
    _recapped(brain.asked(NEXT), RECAP, NEXT)


async def test_the_recap_survives_a_restart_and_is_read_once(tmp_path: Path, brain: Brain) -> None:
    before = await _started(Rig(tmp_path))
    try:
        await _cancelled(before, brain)
        chat = before.backend.session_id
    finally:
        await before.settle()
        await before.stop()

    after = await _started(Rig(tmp_path, resume=chat))
    try:
        await _say(after, NEXT, "m-2")
        await _say(after, LATER, "m-3")
    finally:
        await after.settle()
        await after.stop()

    _recapped(brain.asked(NEXT), RECAP, NEXT)
    _read_before(brain.asked(LATER))


async def test_a_cancel_that_lands_once_the_turn_is_saved_adds_no_recap(
    rig: Rig, brain: Brain
) -> None:
    """The run finished and is in the history; the cancel came before its
    message was marked done, and stamped it cancelled. The model reads the
    turn already: a recap would tell it twice."""
    journal = rig.backend.controllers.runs.pending_journal
    finishing = asyncio.Event()
    mark_completed = journal.mark_completed

    async def held(pending_id: str) -> None:
        finishing.set()
        await asyncio.Event().wait()
        await mark_completed(pending_id)

    journal.mark_completed = held  # type: ignore[method-assign]
    turn = rig.send(msg.UserMessage(id="m-1", text=FINISHED))
    await asyncio.wait_for(finishing.wait(), 10)
    await rig.send(msg.Cancel(id="c-1"))
    await asyncio.wait_for(turn, 30)
    await rig.settle()
    journal.mark_completed = mark_completed  # type: ignore[method-assign]
    assert [r.reason for r in await rig.backend.get_interrupted_runs(rig.backend.session_id)] == [
        "cancelled"
    ]

    await _say(rig, NEXT, "m-2")

    shown = brain.asked(NEXT)
    assert shown.has(FINISHED) and not shown.has(OPENING), shown.lines
    assert await rig.backend.get_interrupted_runs(rig.backend.session_id) == []


async def test_a_retry_of_the_cancelled_turn_adds_no_recap(rig: Rig, brain: Brain) -> None:
    await _cancelled(rig, brain)

    await asyncio.wait_for(rig.send(msg.UserMessage(id="m-2", text=FINISHED, force=True)), 30)
    await rig.settle()

    assert not brain.asked(FINISHED).has(OPENING)


async def test_a_cancel_whose_stamp_is_still_being_written_is_recapped_to_the_next_message(
    rig: Rig, brain: Brain
) -> None:
    journal = rig.backend.controllers.runs.pending_journal
    mark_interrupted = journal.mark_interrupted

    async def slow(pending_id: str, reason: Any, last_error: str | None = None) -> None:
        await asyncio.sleep(0.5)
        await mark_interrupted(pending_id, reason, last_error)

    journal.mark_interrupted = slow  # type: ignore[method-assign]
    await _cancelled(rig, brain)

    await _say(rig, NEXT, "m-2")

    _recapped(brain.asked(NEXT), RECAP, NEXT)


async def test_a_cancelled_turn_that_repeats_an_answered_one_is_still_recapped(
    rig: Rig, brain: Brain
) -> None:
    """The history holds an earlier turn with the same words; that is not this one."""
    await _say(rig, QUESTION, "m-0")
    answered = int(time.time())
    while int(time.time()) == answered:
        await asyncio.sleep(0.05)
    await _cancelled(rig, brain)

    await _say(rig, NEXT, "m-2")

    _recapped(brain.asked(NEXT), RECAP, NEXT)


async def test_a_message_a_hook_blocks_leaves_the_recap_for_the_next(
    rig: Rig, brain: Brain
) -> None:
    await _cancelled(rig, brain)
    gate = rig.backend.controllers.runs._hook_gate  # noqa: SLF001 — a UserPromptSubmit hook
    fire = gate.fire_user_prompt_submit

    async def blocking(text: str) -> HookGateResult:
        if text == "blocked":
            return HookGateResult(should_continue=False, block_message="no")
        return await fire(text)

    gate.fire_user_prompt_submit = blocking  # type: ignore[method-assign]
    await _say(rig, "blocked", "m-2")

    await _say(rig, NEXT, "m-3")

    _recapped(brain.asked(NEXT), RECAP, NEXT)


async def test_two_turns_cancelled_in_a_row_are_both_recapped(rig: Rig, brain: Brain) -> None:
    """The second turn read the first one's recap, but was cut off before it was kept."""
    await _cancelled(rig, brain)
    await _cancelled(rig, brain, SECOND)

    await _say(rig, NEXT, "m-3")

    both = f"{OPENING} The user had pending question(s): {QUESTION + '; ' + SECOND!r}."
    _recapped(brain.asked(NEXT), both, NEXT)


async def test_a_scheduled_run_between_does_not_stand_in_for_the_cancelled_turn(
    rig: Rig, brain: Brain
) -> None:
    await _cancelled(rig, brain)
    await rig.backend.controllers.loop.scheduler.execute("Check the build")

    await _say(rig, NEXT, "m-2")

    _recapped(brain.asked(NEXT), RECAP, NEXT)


async def test_a_recap_whose_turn_paused_is_not_repeated_once_the_person_moves_on(
    rig: Rig, brain: Brain
) -> None:
    """The message that read the recap paused on an approval; the next one ends
    that pause, and the paused turn — recap and all — goes into the history."""
    await _cancelled(rig, brain)
    await _say(rig, "deploy it", "m-2")
    assert rig.waiting(), "paused on the shell call"

    await _say(rig, NEXT, "m-3")

    _read_before(brain.asked(NEXT))
    assert await rig.backend.get_interrupted_runs(rig.backend.session_id) == []
