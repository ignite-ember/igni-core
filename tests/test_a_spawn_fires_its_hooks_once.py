"""One spawn's hooks and its ``agent_started`` event.

``SubagentStop`` fires at most once per spawn, even when the hook fails
or is asked twice; a hook that raises never takes the spawn down; the
progress callback is read when the event is emitted, because the session
wires it on after the toolkit is built.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from igni_core.core.hooks.events import HookEvent
from igni_core.core.tools.orchestrate_events import AgentStartedEvent
from igni_core.core.tools.orchestrate_spawn_hooks import SpawnHooks
from igni_core.core.tools.orchestrate_spawn_types import (
    SubagentStartPayload,
    SubagentStopPayload,
)
from igni_core.core.tools.orchestrate_stream_context import StreamSinks


class _Executor:
    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[tuple[HookEvent, object]] = []
        self.error = error

    async def execute(self, *, event: HookEvent, payload: object) -> None:
        self.calls.append((event, payload))
        if self.error is not None:
            raise self.error


def _stop(error: str | None = None) -> SubagentStopPayload:
    return SubagentStopPayload(session_id="s", agent_name="a", error=error)


class TestSubagentStart:
    async def test_it_hands_the_payload_to_the_executor_as_subagent_start(self):
        executor = _Executor()
        hooks = SpawnHooks(executor, StreamSinks(), "card")
        payload = SubagentStartPayload(session_id="s", agent_name="a", task="t", mode="route")

        await hooks.fire_start(payload)

        assert executor.calls == [(HookEvent.SUBAGENT_START, payload)]


class TestSubagentStopFiresOnce:
    async def test_a_second_stop_does_nothing(self):
        executor = _Executor()
        hooks = SpawnHooks(executor, StreamSinks(), "card")
        first = _stop()

        await hooks.fire_stop_once(first)
        await hooks.fire_stop_once(_stop("late"))

        assert executor.calls == [(HookEvent.SUBAGENT_STOP, first)]
        assert hooks.stop_fired is True

    async def test_a_stop_asked_while_the_hook_runs_does_not_fire_it_again(self):
        """The flag is set before the hook runs, so a stop that arrives
        meanwhile (a cancellation racing the normal end) is the second."""
        hooks: SpawnHooks

        class _Reentrant(_Executor):
            async def execute(self, *, event: HookEvent, payload: object) -> None:
                await super().execute(event=event, payload=payload)
                await hooks.fire_stop_once(_stop("raced"))

        executor = _Reentrant()
        hooks = SpawnHooks(executor, StreamSinks(), "card")

        await hooks.fire_stop_once(_stop())

        assert len(executor.calls) == 1

    async def test_it_has_not_fired_before_the_first_stop(self):
        assert SpawnHooks(_Executor(), StreamSinks(), "card").stop_fired is False

    async def test_a_failing_hook_still_uses_up_the_one_stop(self):
        executor = _Executor(RuntimeError("hook broke"))
        hooks = SpawnHooks(executor, StreamSinks(), "card")

        await hooks.fire_stop_once(_stop())
        await hooks.fire_stop_once(_stop())

        assert len(executor.calls) == 1
        assert hooks.stop_fired is True

    async def test_stop_is_consumed_even_without_an_executor(self):
        hooks = SpawnHooks(None, StreamSinks(), "card")

        await hooks.fire_stop_once(_stop())

        assert hooks.stop_fired is True


class TestAHookObservesTheSpawn:
    async def test_without_an_executor_nothing_happens(self):
        hooks = SpawnHooks(None, StreamSinks(), "card")

        await hooks.fire_hook(HookEvent.SUBAGENT_START, _stop())  # must not raise

    async def test_a_raising_executor_is_logged_not_propagated(self, caplog):
        hooks = SpawnHooks(_Executor(RuntimeError("hook broke")), StreamSinks(), "card")

        with caplog.at_level(logging.WARNING, logger="igni_core.llm_calls"):
            await hooks.fire_hook(HookEvent.SUBAGENT_START, _stop())

        assert "spawn hook SubagentStart failed: hook broke" in caplog.text

    async def test_cancellation_inside_a_hook_is_not_swallowed(self):
        hooks = SpawnHooks(_Executor(asyncio.CancelledError()), StreamSinks(), "card")

        with pytest.raises(asyncio.CancelledError):
            await hooks.fire_hook(HookEvent.SUBAGENT_STOP, _stop())


class TestAgentStarted:
    def test_the_event_carries_the_agent_the_task_and_the_card(self):
        seen: list[AgentStartedEvent] = []
        hooks = SpawnHooks(None, StreamSinks(on_progress=seen.append), "card42")

        hooks.emit_agent_started("reviewer", "check ünïcode " + "x" * 2000)

        assert len(seen) == 1
        event = seen[0]
        assert isinstance(event, AgentStartedEvent)
        assert event.agent_path == "reviewer"
        assert event.agent == "reviewer"
        assert event.parent is None
        assert event.task == "check ünïcode " + "x" * 2000
        assert event.card_id == "card42"
        assert hooks.card_id == "card42"

    def test_without_a_callback_it_is_a_no_op(self):
        SpawnHooks(None, StreamSinks(), "c").emit_agent_started("a", "t")  # must not raise

    def test_a_callback_wired_after_construction_still_hears_it(self):
        sinks = StreamSinks()
        hooks = SpawnHooks(None, sinks, "c")
        seen: list[object] = []

        sinks.on_progress = seen.append
        hooks.emit_agent_started("a", "t")

        assert len(seen) == 1

    def test_a_raising_callback_is_logged_not_propagated(self, caplog):
        def boom(_event: object) -> None:
            raise RuntimeError("ui gone")

        hooks = SpawnHooks(None, StreamSinks(on_progress=boom), "c")

        with caplog.at_level(logging.WARNING, logger="igni_core.llm_calls"):
            hooks.emit_agent_started("a", "t")

        assert "spawn progress callback failed: ui gone" in caplog.text
