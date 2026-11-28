"""Session 6fa3e09e, replayed in its exact shape against the real backend."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator

import pytest

from tests.session_lifecycle.invariant_checker import InvariantChecker
from tests.session_lifecycle.lifecycle_rig import RIG_CONFIG, LifecycleRig
from tests.session_lifecycle.schemas.model_turn import ModelTurn
from tests.session_lifecycle.schemas.scripted_tool_call import ScriptedToolCall
from tests.session_lifecycle.verdict import Verdict

pytestmark = [pytest.mark.session_lifecycle]

CELL = "incident.6fa3e09e"
MARK = "[6fa3e09e]"
FIRST = f"what about the CV claims {MARK}"
DREAM = f"Who did I have a dream speech {MARK}"
FOLLOW_UP = f"No, search internet for this one {MARK}"
PROBE = f"so what did you find {MARK}"


def _shell(command: str) -> ScriptedToolCall:
    return ScriptedToolCall(name="run_shell_command", arguments={"command": command})


@pytest.fixture(scope="module")
def rig(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LifecycleRig]:
    rig = LifecycleRig(tmp_path_factory.mktemp("incident"), extra_config=RIG_CONFIG)
    script = rig.script
    script.add(FIRST, ModelTurn(text="The CV claims check out against the repository."))
    long_turn = [
        ModelTurn(text=f"Searching, step {i}.", tool_calls=[_shell("sleep 0"), _shell("sleep 0")])
        for i in range(5)
    ]
    script.add(
        DREAM,
        *long_turn,
        ModelTurn(text="Let me search the home folder.", tool_calls=[_shell("rg -i dream ~")]),
        ModelTurn(text="I could not find it."),
    )
    script.add(
        FOLLOW_UP,
        ModelTurn(text="Searching the web for the speech.", tool_calls=[_shell("sleep 0")]),
        ModelTurn(text="Martin Luther King Jr. gave it. " * 6, hold=f"follow-up {MARK}"),
    )
    script.add(PROBE, ModelTurn(text="Here is what I found."))
    rig.start()
    try:
        yield rig
    finally:
        rig.stop()


async def test_the_follow_up_sees_the_paused_turn_and_stop_stops_everything(
    rig: LifecycleRig, request: pytest.FixtureRequest
) -> None:
    Verdict.expect_known(request, CELL)
    checker = InvariantChecker(rig, MARK)
    client = await rig.new_session("incident")
    try:
        checker.expect(FIRST)
        first = await client.send(FIRST)
        await client.wait_for(lambda f: f.type == "stream_end" and f.id == first, since=0)
        checker.mark("completed")
        await client.quiet()

        checker.expect(DREAM)
        await client.send(DREAM)
        await client.wait_for(lambda f: f.type == "run_paused", since=0, timeout=40)
        await client.quiet()
        checker.mark("paused")
        checker.check_requests("the long turn paused")
        await checker.check_settled("the long turn paused", client)

        # The window is waiting on an approval, so the composer sends, not queues.
        assert not client.view.queues_messages
        checker.expect(FOLLOW_UP)
        await client.follow_up(FOLLOW_UP)
        rig.model.wait_for(
            lambda rs: any(
                r.rule == FOLLOW_UP and r.step >= 1 and r.outcome == "streaming" for r in rs
            ),
            timeout=20,
        )
        checker.check_requests("follow-up while paused")
        checker.check_zombies("follow-up while paused", client)

        mark = len(client.frames)
        cancelled_at = time.monotonic()
        for _ in range(3):
            await client.stop()
            await asyncio.sleep(0.3)
        checker.mark("cancelled")
        rig.model.release(f"follow-up {MARK}")
        await checker.check_cancelled("three Stops", client, cancelled_at, mark)
        checker.check_zombies("three Stops", client)

        checker.expect(PROBE)
        probe = await client.send(PROBE)
        try:
            await client.wait_for(
                lambda f: f.type == "stream_end" and f.id == probe, since=0, timeout=25
            )
        except TimeoutError:
            checker.fail("answers", "probe", "the next message was never answered")
        await client.quiet()
        checker.check_requests("probe")
        checker.check_stream_ends("probe", client)
        await checker.check_settled("probe", client)
    finally:
        rig.model.release(f"follow-up {MARK}")
        await rig.close_windows()
    Verdict.settle(CELL, checker.violations)
