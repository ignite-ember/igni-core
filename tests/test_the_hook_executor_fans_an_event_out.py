"""How ``HookExecutor.execute`` fans one event out to its hooks and what it hands back.

Real command hooks are run. Foreground hooks run side by side and merge (a
block wins); a background hook is fire-and-forget, except at ``SessionEnd``,
where nothing runs after it and it is waited for, within the slowest hook's own
timeout plus a second of slack. A hook that raises or that no handler
implements is skipped.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.executor import (
    HookExecutor,
    _hook_result_from_envelope,
    _matcher_matches,
)
from igni_core.core.hooks.extension_gate import extension_hook_gate
from igni_core.core.hooks.schemas import HookDefinition, HookPayload, PermissionDecision
from igni_core.core.session.schemas import SessionLifecyclePayload

START = HookEvent.SESSION_START.value
END = HookEvent.SESSION_END.value


def _payload() -> SessionLifecyclePayload:
    return SessionLifecyclePayload(session_id="s")


def _command(command: str, **fields: Any) -> HookDefinition:
    return HookDefinition(type="command", command=command, **fields)


class Listener:
    """An extension host that hooks every event and records what it is told."""

    def __init__(self) -> None:
        self.told: list[tuple[HookEvent, HookPayload]] = []

    def hooks(self, event: HookEvent) -> bool:
        return True

    async def ask(self, value: Any) -> Any:  # pragma: no cover - never asked here
        raise AssertionError("the executor does not ask")

    def tell(self, event: HookEvent, payload: HookPayload) -> None:
        self.told.append((event, payload))

    def tool_called(self, tool_name: str, *, answered: bool, detail: str) -> None:
        return None


@pytest.fixture
def listener():
    heard = Listener()
    extension_hook_gate.install(heard)
    try:
        yield heard
    finally:
        extension_hook_gate.install(None)


class TestWhatComesBack:
    async def test_no_hooks_at_all_is_a_continue(self):
        result = await HookExecutor({}).execute(START, _payload())

        assert result.should_continue is True
        assert result.message == ""
        assert result.permission_decision == PermissionDecision.NONE

    async def test_hooks_for_another_event_or_target_are_a_continue(self, tmp_path: Path):
        marker = tmp_path / "ran"
        executor = HookExecutor(
            {
                END: [_command(f"touch {marker}")],
                "PreToolUse": [_command(f"touch {marker}", matcher="Bash")],
            }
        )

        assert (await executor.execute(START, _payload())).should_continue is True
        assert (await executor.execute("PreToolUse", _payload(), target="Read")).should_continue
        assert not marker.exists()

    async def test_a_blocking_hook_wins_over_the_others_and_messages_join(self):
        executor = HookExecutor(
            {
                START: [
                    _command("echo fine"),
                    _command("echo stop-it >&2; exit 2"),
                ]
            }
        )

        result = await executor.execute(START, _payload())

        assert result.should_continue is False
        assert "stop-it" in result.message

    async def test_foreground_hooks_run_side_by_side(self):
        executor = HookExecutor({START: [_command("sleep 0.6"), _command("sleep 0.6")]})
        loop = asyncio.get_running_loop()

        started = loop.time()
        await executor.execute(START, _payload())

        assert loop.time() - started < 1.1

    async def test_a_hook_of_a_type_nobody_implements_is_skipped(self, tmp_path: Path):
        marker = tmp_path / "ran"
        executor = HookExecutor(
            {START: [HookDefinition(type="agnet"), _command(f"touch {marker}")]}
        )

        result = await executor.execute(START, _payload())

        assert result.should_continue is True
        assert marker.exists()

    async def test_every_foreground_hook_unknown_is_a_continue(self):
        executor = HookExecutor({START: [HookDefinition(type="agnet")]})

        result = await executor.execute(START, _payload())

        assert result.should_continue is True

    async def test_a_hook_that_raises_is_skipped_in_the_merge(self, monkeypatch):
        executor = HookExecutor({START: [_command("echo boom"), _command("echo stop >&2; exit 2")]})
        real = executor._handler_registry.run

        async def run(hook: HookDefinition, event: str, payload: HookPayload):
            if hook.command == "echo boom":
                raise RuntimeError("handler exploded")
            return await real(hook, event, payload)

        monkeypatch.setattr(executor._handler_registry, "run", run)

        result = await executor.execute(START, _payload())

        assert result.should_continue is False
        assert "stop" in result.message


class TestBackgroundAndRewake:
    async def test_a_background_hook_does_not_hold_the_event_up_or_merge(self):
        executor = HookExecutor({START: [_command("sleep 0.5; exit 2", background=True)]})
        loop = asyncio.get_running_loop()

        started = loop.time()
        result = await executor.execute(START, _payload())

        assert loop.time() - started < 0.4
        assert result.should_continue is True
        await executor.drain_background(5.0)

    async def test_a_rewake_hook_is_run_in_the_background_and_not_merged(self, tmp_path: Path):
        marker = tmp_path / "ran"
        executor = HookExecutor(
            {START: [_command(f"sleep 0.3; touch {marker}; exit 2", async_rewake=True)]}
        )
        loop = asyncio.get_running_loop()

        started = loop.time()
        result = await executor.execute(START, _payload())

        assert loop.time() - started < 0.25
        assert result.should_continue is True
        assert not marker.exists()
        await executor.drain_background(5.0)
        assert marker.exists()

    async def test_session_end_waits_for_a_background_hook_but_session_start_does_not(
        self, tmp_path: Path
    ):
        ended, started = tmp_path / "ended", tmp_path / "started"
        executor = HookExecutor(
            {
                END: [_command(f"sleep 0.3; touch {ended}", background=True)],
                START: [_command(f"sleep 0.3; touch {started}", background=True)],
            }
        )

        try:
            await executor.execute(START, _payload())
            assert not started.exists()
            assert [t for t in executor._background if not t.done()]

            await executor.execute(END, _payload())
            assert ended.exists()
        finally:
            for task in list(executor._background):
                task.cancel()
            await asyncio.sleep(0.05)

    async def test_a_running_background_task_is_held_until_it_is_done(self):
        executor = HookExecutor({START: [_command("sleep 0.3", background=True)]})

        await executor.execute(START, _payload())
        try:
            held = len(executor._background)
        finally:
            await asyncio.sleep(0.5)

        assert held == 1

    async def test_the_tasks_are_forgotten_once_done(self):
        executor = HookExecutor({START: [_command("true", background=True)]})

        await executor.execute(START, _payload())
        await executor.drain_background(5.0)
        await asyncio.sleep(0)

        assert executor._background == set()

    async def test_draining_with_nothing_pending_returns_at_once_and_says_nothing(self, caplog):
        executor = HookExecutor({})
        loop = asyncio.get_running_loop()

        with caplog.at_level(logging.WARNING):
            started = loop.time()
            await executor.drain_background(30.0)

        assert loop.time() - started < 0.1
        assert caplog.text == ""

    async def test_draining_names_how_many_hooks_it_abandons(self, caplog):
        executor = HookExecutor(
            {
                START: [
                    _command("sleep 5", background=True),
                    _command("sleep 5", background=True),
                ]
            }
        )
        await executor.execute(START, _payload())

        with caplog.at_level(logging.WARNING):
            await executor.drain_background(0.05)

        assert "2 background hook(s) still running after 0.1s" in caplog.text
        for task in list(executor._background):
            task.cancel()
        await asyncio.sleep(0.05)

    @pytest.mark.parametrize(
        ("timeouts", "seconds"),
        [([], 1.0), ([0], 1.0), ([2000], 3.0), ([500, 4000, 1000], 5.0)],
    )
    def test_the_drain_budget_is_the_slowest_timeout_plus_a_second(
        self, timeouts: list[int], seconds: float
    ):
        hooks = [_command("true", timeout=t) for t in timeouts]

        assert HookExecutor._drain_deadline(hooks) == seconds


class TestToldToExtensions:
    async def test_the_extensions_that_hook_the_event_are_told_the_typed_payload(self, listener):
        payload = _payload()

        await HookExecutor({}).execute(START, payload)

        assert len(listener.told) == 1
        event, told = listener.told[0]
        assert event == HookEvent.SESSION_START
        assert isinstance(told, HookPayload)
        assert told.body is payload

    async def test_they_are_told_even_when_no_configured_hook_matches(self, listener):
        executor = HookExecutor({END: [_command("true")]})

        await executor.execute(START, _payload())

        assert len(listener.told) == 1

    async def test_an_event_extensions_decide_is_not_told_from_here(self, listener):
        await HookExecutor({}).execute(HookEvent.STOP.value, _payload())

        assert listener.told == []


class TestMatching:
    @pytest.mark.parametrize(
        ("matcher", "target", "expected"),
        [
            ("", "Bash", True),
            ("*", "Bash", True),
            ("Bash", "Bash", True),
            ("Bash", "BashOutput", False),
            ("Read|Write", "Write", True),
            ("Read|Write", "Edit", False),
            ("mcp__.*", "mcp__fs__read", True),
            ("mcp__.*", "Bash", False),
        ],
    )
    def test_the_executor_matches_by_the_registrys_rules(
        self, matcher: str, target: str, expected: bool
    ):
        hook = _command("true", matcher=matcher)
        executor = HookExecutor({"PreToolUse": [hook]})

        found = executor.get_matching_hooks("PreToolUse", target)

        assert (found == [hook]) is expected
        assert _matcher_matches(matcher, target) is expected


class TestTheEnvelopeShim:
    def test_a_dict_is_read_as_an_envelope(self):
        result = _hook_result_from_envelope(
            {"continue": False, "systemMessage": "no", "permissionDecision": "deny"}
        )

        assert result.should_continue is False
        assert result.message == "no"
        assert result.permission_decision == PermissionDecision.DENY

    def test_none_is_a_quiet_continue(self):
        result = _hook_result_from_envelope(None)

        assert (result.should_continue, result.message) == (True, "")

    @pytest.mark.parametrize(("raw", "text"), [("hello", "hello"), ([1, 2], "[1, 2]"), (7, "7")])
    def test_anything_else_is_a_continue_with_its_text(self, raw: Any, text: str):
        result = _hook_result_from_envelope(raw)

        assert result.should_continue is True
        assert result.message == text
