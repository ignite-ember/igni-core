"""A cancel that lands while a frame is being sent still ends the run.

``cancel_run`` cancels the task streaming the run to the windows. That task
spends much of its time sending frames, and a cancel that landed on a send
was raised outside the run: the run never heard it. Its clean-up never ran
— the session stayed ``running`` for every window, the team's run went on
— and the stream simply ended. Session ``6fa3e09e``'s window, which offers
Stop whenever the backend says the session has a run going, kept Stop up
for good after a Stop that rejected an approval.

The windows here take a moment over every frame, as a socket does, so the
cancel lands on a send. Each test runs the orchestrator the app builds over
a real backend, with only the team replaced.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from agno.models.response import ToolExecution
from agno.run.agent import RunContentEvent, RunPausedEvent
from agno.run.requirement import RunRequirement

from igni_core.backend.activity import ActivityState
from igni_core.backend.server import BackendServer
from igni_core.core.config.settings_loader import SettingsLoader
from igni_core.protocol import messages as msg
from igni_core.protocol.schemas.envelope import Standing
from tests.host_model_helpers import HostRig

STATES = "session_states_changed"


class _Team:
    """Pauses on a shell call; continued, or started working, it streams until stopped."""

    instructions: str | None = None
    db: Any = None

    def __init__(self, *, working: bool = False) -> None:
        self._working = working
        self.stopped = 0

    async def arun(self, *_: Any, **__: Any) -> AsyncIterator[Any]:
        if self._working:
            async for event in self._stream("run-1"):
                yield event
            return
        yield RunPausedEvent(
            run_id="run-1",
            requirements=[
                RunRequirement(
                    tool_execution=ToolExecution(
                        tool_call_id="call-1",
                        tool_name="run_shell_command",
                        tool_args={"command": "make deploy"},
                        requires_confirmation=True,
                    )
                )
            ],
        )

    async def acontinue_run(self, **kwargs: Any) -> AsyncIterator[Any]:
        async for event in self._stream(kwargs["run_id"]):
            yield event

    async def _stream(self, run_id: str) -> AsyncIterator[Any]:
        try:
            for i in range(100_000):
                yield RunContentEvent(content=f"{i} ", run_id=run_id)
                await asyncio.sleep(0)
        except (asyncio.CancelledError, GeneratorExit):
            self.stopped += 1
            raise


class _Rig(HostRig):
    def __init__(self, tmp_path: Path, team: _Team) -> None:
        super().__init__(tmp_path)
        backend = BackendServer(
            SettingsLoader.load(project_dir=self.project), project_dir=self.project
        )
        backend._session.main_team = team  # type: ignore[assignment]
        self.backend = backend
        self.session = backend  # type: ignore[assignment]
        windows = self.windows

        async def slow_send(message: msg.Message) -> None:
            await asyncio.sleep(0.002)
            windows.sent.append(message)

        windows.send = slow_send  # type: ignore[method-assign]


def _from_window(message: msg.Message) -> msg.Message:
    message.mark_from("ws-1", Standing.WINDOW)
    return message


async def _until(ready: Any) -> None:
    for _ in range(1000):
        if ready():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("never reached")


def _last_state(rig: _Rig) -> str:
    pushes = [
        m.payload
        for m in rig.windows.sent
        if isinstance(m, msg.PushNotification) and m.channel == STATES
    ]
    assert pushes, "the windows were told the chats' states"
    rows = [r for r in pushes[-1]["sessions"] if r["session_id"] == rig.backend.session_id]
    return rows[0]["state"] if rows else "idle"


async def _stopped_and_idle(rig: _Rig, team: _Team) -> None:
    await rig.settle()
    await _until(lambda: team.stopped == 1)
    assert not rig.backend.processing
    assert rig.backend.activity().state is ActivityState.IDLE
    assert _last_state(rig) == "idle", "every window is told the chat is idle"


async def test_a_reject_then_a_cancel_leaves_the_session_idle(tmp_path: Path) -> None:
    """What the window's Stop sends while an approval waits: the rejection, then the cancel."""
    team = _Team()
    rig = await _Rig(tmp_path, team).start()
    try:
        assert rig.orchestrator is not None
        await rig.orchestrator.dispatch(_from_window(msg.UserMessage(id="m-1", text="deploy")))
        [paused] = rig.backend.controllers.hitl.waiting_messages()
        requirement_id = paused.requirements[0].requirement_id
        rig.orchestrator.spawn_dispatch(
            _from_window(
                msg.HITLResponseBatch(
                    id="h-1",
                    decisions=[msg.HITLDecision(requirement_id=requirement_id, action="confirm")],
                )
            )
        )
        await _until(lambda: sum(isinstance(m, msg.ContentDelta) for m in rig.windows.sent) > 5)

        await rig.orchestrator.dispatch(_from_window(msg.Cancel(id="c-1")))

        await _stopped_and_idle(rig, team)
    finally:
        await rig.stop()


async def test_a_cancel_during_a_working_turn_leaves_the_session_idle(tmp_path: Path) -> None:
    team = _Team(working=True)
    rig = await _Rig(tmp_path, team).start()
    try:
        assert rig.orchestrator is not None
        rig.orchestrator.spawn_dispatch(_from_window(msg.UserMessage(id="m-1", text="work")))
        await _until(lambda: sum(isinstance(m, msg.ContentDelta) for m in rig.windows.sent) > 5)

        await rig.orchestrator.dispatch(_from_window(msg.Cancel(id="c-1")))

        await _stopped_and_idle(rig, team)
        assert any(
            isinstance(m, msg.Info) and m.text == "Run cancelled by user." for m in rig.windows.sent
        ), "the stream says it was cancelled"
    finally:
        await rig.stop()
