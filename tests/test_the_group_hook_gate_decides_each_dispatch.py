"""What ``GroupHookGate`` answers for one dispatch of a hook, and what it says about it.

``hooks.group`` is ``run``, ``ask`` or ``refuse``. Anything the gate cannot
answer is a refusal, nothing it was not told is remembered, and the person is
put one question per version of a hook however many dispatches are waiting.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from igni_core.core.hooks.group_hook_decisions import GroupHookDecisions
from igni_core.core.hooks.group_hook_gate import (
    APPROVAL_NAME,
    APPROVAL_REASON,
    GroupHookApprovalDetails,
    GroupHookGate,
)
from igni_core.core.hooks.schemas import HookDefinition


def _hook(command: str = "echo hi") -> HookDefinition:
    return HookDefinition(type="command", command=command).mark_from_group("/g/hooks.json")


def _store(tmp_path: Path) -> GroupHookDecisions:
    return GroupHookDecisions(tmp_path / "decisions.json")


class Question:
    """An approval channel that holds each question until it is answered."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, dict[str, Any], str, list[str]]] = []
        self.release = asyncio.Event()
        self.answer: bool | None = True

    async def __call__(
        self, name: str, details: dict[str, Any], reason: str, path: list[str]
    ) -> bool | None:
        self.asked.append((name, details, reason, path))
        await self.release.wait()
        return self.answer


class TestWhatPermitsAnswers:
    async def test_a_hook_the_group_did_not_ship_runs_in_every_mode(self, tmp_path: Path):
        own = HookDefinition(type="command", command="echo mine")
        for mode in ("run", "ask", "refuse"):
            gate = GroupHookGate(mode=mode, decisions=_store(tmp_path))  # type: ignore[arg-type]

            assert await gate.permits(own, "SessionStart") is True

    async def test_run_lets_a_group_hook_run_and_says_nothing(self, tmp_path: Path, caplog):
        gate = GroupHookGate(mode="run", decisions=_store(tmp_path))

        with caplog.at_level(logging.INFO):
            assert await gate.permits(_hook(), "SessionStart") is True

        assert caplog.text == ""

    async def test_refuse_says_which_hook_from_where_for_which_event(self, tmp_path: Path, caplog):
        gate = GroupHookGate(mode="refuse", decisions=_store(tmp_path))

        with caplog.at_level(logging.WARNING):
            assert await gate.permits(_hook("echo x"), "PreToolUse") is False

        assert (
            "Not running the group policy's PreToolUse hook from /g/hooks.json "
            "(hooks.group: refuse): echo x"
        ) in caplog.text

    async def test_the_same_refusal_is_logged_once_per_hook(self, tmp_path: Path, caplog):
        gate = GroupHookGate(mode="refuse", decisions=_store(tmp_path))

        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                await gate.permits(_hook("echo x"), "PreToolUse")
            await gate.permits(_hook("echo other"), "PreToolUse")

        assert caplog.text.count("echo x") == 1
        assert caplog.text.count("echo other") == 1

    async def test_the_mode_is_read_on_every_call(self, tmp_path: Path):
        mode = ["refuse"]
        gate = GroupHookGate(mode=lambda: mode[0], decisions=_store(tmp_path))  # type: ignore[arg-type]

        assert await gate.permits(_hook(), "SessionStart") is False
        mode[0] = "run"
        assert await gate.permits(_hook(), "SessionStart") is True

    async def test_an_unrecognised_mode_reads_as_ask(self, tmp_path: Path):
        gate = GroupHookGate(mode="sometimes", decisions=_store(tmp_path))  # type: ignore[arg-type]

        assert gate.mode() == "ask"
        assert await gate.permits(_hook(), "SessionStart") is False

    async def test_an_unwired_gate_refuses_and_remembers_nothing(self, tmp_path: Path):
        gate = GroupHookGate.unwired()
        hook = _hook()

        assert gate.mode() == "refuse"
        assert await gate.permits(hook, "SessionStart") is False
        assert gate._decisions.get(hook.digest()) is None


class TestAsking:
    async def test_the_dialog_gets_the_name_the_details_the_reason_and_the_path(
        self, tmp_path: Path
    ):
        question = Question()
        question.release.set()
        gate = GroupHookGate(mode="ask", ask=question, decisions=_store(tmp_path))

        await gate.permits(_hook("echo x"), "PreToolUse")

        assert question.asked == [
            (
                APPROVAL_NAME,
                {
                    "hook": "echo x",
                    "type": "command",
                    "event": "PreToolUse",
                    "source": "/g/hooks.json",
                },
                APPROVAL_REASON,
                ["group policy hook"],
            )
        ]
        assert APPROVAL_NAME == "group_policy_hook"

    def test_the_details_describe_the_hook(self):
        details = GroupHookApprovalDetails.of(
            HookDefinition(type="http", url="http://x").mark_from_group("g"), "Stop"
        )

        assert details.model_dump() == {
            "hook": "POST http://x",
            "type": "http",
            "event": "Stop",
            "source": "g",
        }

    async def test_a_yes_is_kept_as_run_and_a_no_as_refuse(self, tmp_path: Path):
        store = _store(tmp_path)
        yes, no = _hook("echo yes"), _hook("echo no")
        for hook, answer in ((yes, True), (no, False)):
            question = Question()
            question.answer = answer
            question.release.set()
            gate = GroupHookGate(mode="ask", ask=question, decisions=store)

            assert await gate.permits(hook, "SessionStart") is answer

        assert store.get(yes.digest()) == "run"
        assert store.get(no.digest()) == "refuse"

    async def test_a_remembered_refusal_is_honoured_without_asking_and_logged_once(
        self, tmp_path: Path, caplog
    ):
        store = _store(tmp_path)
        hook = _hook("echo x")
        store.set(hook.digest(), "refuse", hook)
        question = Question()
        question.release.set()
        gate = GroupHookGate(mode="ask", ask=question, decisions=store)

        with caplog.at_level(logging.WARNING):
            assert await gate.permits(hook, "SessionStart") is False
            assert await gate.permits(hook, "SessionStart") is False

        assert question.asked == []
        assert caplog.text.count("(you declined it): echo x") == 1

    async def test_a_remembered_run_is_honoured_without_asking(self, tmp_path: Path):
        store = _store(tmp_path)
        hook = _hook()
        store.set(hook.digest(), "run", hook)
        question = Question()
        gate = GroupHookGate(mode="ask", ask=question, decisions=store)

        assert await gate.permits(hook, "SessionStart") is True
        assert question.asked == []

    async def test_nobody_to_answer_says_so_once_and_remembers_nothing(
        self, tmp_path: Path, caplog
    ):
        store = _store(tmp_path)
        hook = _hook("echo x")
        gate = GroupHookGate(mode="ask", ask=None, decisions=store)

        with caplog.at_level(logging.WARNING):
            assert await gate.permits(hook, "Stop") is False
            assert await gate.permits(hook, "Stop") is False

        assert caplog.text.count("nobody is here to answer") == 1
        assert store.get(hook.digest()) is None

    async def test_a_channel_that_answers_nothing_is_a_refusal_not_remembered(self, tmp_path: Path):
        store = _store(tmp_path)
        hook = _hook()
        question = Question()
        question.answer = None
        question.release.set()
        gate = GroupHookGate(mode="ask", ask=question, decisions=store)

        assert await gate.permits(hook, "SessionStart") is False
        assert store.get(hook.digest()) is None

    async def test_a_dispatch_waiting_on_the_question_gets_the_askers_answer(self, tmp_path: Path):
        question = Question()
        gate = GroupHookGate(mode="ask", ask=question, decisions=_store(tmp_path))
        hook = _hook()

        first = asyncio.ensure_future(gate.permits(hook, "SessionStart"))
        await asyncio.sleep(0)
        second = asyncio.ensure_future(gate.permits(hook, "PreToolUse"))
        await asyncio.sleep(0)
        question.answer = True
        question.release.set()

        assert await asyncio.gather(first, second) == [True, True]
        assert len(question.asked) == 1

    async def test_a_cancelled_asker_refuses_those_waiting_and_a_later_dispatch_asks_again(
        self, tmp_path: Path
    ):
        question = Question()
        store = _store(tmp_path)
        gate = GroupHookGate(mode="ask", ask=question, decisions=store)
        hook = _hook()

        first = asyncio.ensure_future(gate.permits(hook, "SessionStart"))
        await asyncio.sleep(0)
        second = asyncio.ensure_future(gate.permits(hook, "PreToolUse"))
        await asyncio.sleep(0)
        first.cancel()

        assert await second is False
        with pytest.raises(asyncio.CancelledError):
            await first
        assert store.get(hook.digest()) is None

        question.release.set()
        assert await gate.permits(hook, "SessionStart") is True
        assert len(question.asked) == 2
