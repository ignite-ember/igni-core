"""A session's wind-down runs its steps in order, and one that fails does not stop the rest.

``ShutdownPipeline`` is driven directly over a session whose hook executor,
pool and MCP manager record what they were asked, so the order, the gate on
ephemeral-agent cleanup, the scheduler's absence and the errors in the
result are pinned.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.backend.server_shutdown import ShutdownPipeline
from igni_core.core.hooks.events import HookEvent


class Rig:
    def __init__(
        self, *, auto_cleanup: bool = True, has_ephemeral: bool = True, scheduler: bool = True
    ) -> None:
        self.calls: list[str] = []
        self.hook_calls: list[dict[str, Any]] = []
        self.fail: dict[str, BaseException] = {}
        rig = self

        class Hooks:
            async def execute(self, *, event: str, payload: Any) -> None:
                rig.hook_calls.append({"event": event, "payload": payload})
                rig._step("hook")

        class Ephemeral:
            def cleanup(self) -> None:
                rig._step("ephemeral")

        class Mcp:
            async def disconnect_all(self) -> None:
                rig._step("mcp")

        async def stop_scheduler() -> None:
            rig._step("scheduler")

        self.session = SimpleNamespace(
            session_id="s-9",
            hook_executor=Hooks(),
            settings=SimpleNamespace(orchestration=SimpleNamespace(auto_cleanup=auto_cleanup)),
            pool=SimpleNamespace(has_ephemeral=has_ephemeral, ephemeral=Ephemeral()),
            mcp_manager=Mcp(),
        )
        self.pipeline = ShutdownPipeline(
            self.session,  # type: ignore[arg-type]
            stop_scheduler if scheduler else None,
        )

    def _step(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            raise self.fail[name]


async def test_every_step_runs_in_order_and_the_result_says_so() -> None:
    rig = Rig()

    result = await rig.pipeline.run()

    assert rig.calls == ["hook", "ephemeral", "mcp", "scheduler"]
    assert (
        result.hook_fired,
        result.pool_cleaned,
        result.mcp_disconnected,
        result.scheduler_stopped,
    ) == (True, True, True, True)
    assert result.errors == []


async def test_session_end_carries_the_session_s_id() -> None:
    rig = Rig()

    await rig.pipeline.run()

    [call] = rig.hook_calls
    assert call["event"] == HookEvent.SESSION_END.value
    assert call["payload"].session_id == "s-9"


@pytest.mark.parametrize(
    ("auto_cleanup", "has_ephemeral"), [(False, True), (True, False), (False, False)]
)
async def test_ephemeral_agents_are_cleaned_only_when_gate_and_agents_are_there(
    auto_cleanup: bool, has_ephemeral: bool
) -> None:
    rig = Rig(auto_cleanup=auto_cleanup, has_ephemeral=has_ephemeral)

    result = await rig.pipeline.run()

    assert "ephemeral" not in rig.calls
    assert result.pool_cleaned, "the step is done either way"


async def test_without_a_scheduler_the_step_is_absent() -> None:
    rig = Rig(scheduler=False)

    result = await rig.pipeline.run()

    assert rig.calls == ["hook", "ephemeral", "mcp"]
    assert result.scheduler_stopped is False
    assert result.errors == [], "an absent step is not a failed one"


@pytest.mark.parametrize(
    ("failing", "label", "unmarked"),
    [
        ("hook", "session_end_hook", "hook_fired"),
        ("ephemeral", "pool_cleanup", "pool_cleaned"),
        ("mcp", "mcp_disconnect", "mcp_disconnected"),
        ("scheduler", "scheduler_stop", "scheduler_stopped"),
    ],
)
async def test_a_failing_step_is_recorded_and_the_rest_still_run(
    failing: str, label: str, unmarked: str
) -> None:
    rig = Rig()
    rig.fail[failing] = RuntimeError("boom")

    result = await rig.pipeline.run()

    assert rig.calls == ["hook", "ephemeral", "mcp", "scheduler"]
    assert result.errors == [f"{label}: boom"]
    assert getattr(result, unmarked) is False
    others = {"hook_fired", "pool_cleaned", "mcp_disconnected", "scheduler_stopped"} - {unmarked}
    assert all(getattr(result, name) for name in others)


async def test_a_cancelled_wind_down_is_not_swallowed() -> None:
    rig = Rig()
    rig.fail["hook"] = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await rig.pipeline.run()
    assert rig.calls == ["hook"]
