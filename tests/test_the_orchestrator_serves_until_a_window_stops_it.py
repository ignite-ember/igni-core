"""The serve loop and the crash boundary of the one backend, over a real pool.

Messages arrive on a scripted transport. Only igni's window may stop igni:
a program's ``Shutdown`` is refused and the loop goes on. A message that
crashes while being routed becomes an ``Error`` for the windows, and the next
one is handled as usual — even when telling the windows fails too. What is in
flight when shutdown arrives is let finish, and a post from an extension to a
session the pool has not made yet makes it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from igni_core.backend.schemas_pool import ExtensionPost
from igni_core.protocol import messages as msg
from igni_core.protocol.schemas.envelope import Standing
from tests.host_model_helpers import HostRig


@pytest.fixture
async def rig(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    try:
        yield rig
    finally:
        await rig.stop()


def script(rig: HostRig, *messages: msg.Message) -> None:
    async def receive() -> AsyncIterator[msg.Message]:
        for message in messages:
            yield message

    rig.windows.receive = receive  # type: ignore[method-assign]


def from_(standing: Standing, message: msg.Message) -> msg.Message:
    message.mark_from("ws-1", standing)
    return message


def info_request(request_id: str) -> msg.SessionList:
    return msg.SessionList(id=request_id)


def answered(rig: HostRig, request_id: str) -> bool:
    return any(m.id == request_id for m in rig.windows.of(msg.SessionListResult))


async def test_a_shutdown_from_a_program_is_refused_and_the_loop_goes_on(rig: HostRig) -> None:
    assert rig.orchestrator is not None
    script(
        rig,
        from_(Standing.PROGRAM, msg.Shutdown(id="stop-1")),
        from_(Standing.WINDOW, info_request("after")),
        from_(Standing.WINDOW, msg.Shutdown(id="stop-2")),
        from_(Standing.WINDOW, info_request("never")),
    )

    await rig.orchestrator.serve(asyncio.Event())

    [refused] = [m for m in rig.windows.of(msg.Error) if m.id == "stop-1"]
    assert "only igni's window may stop igni" in refused.text
    assert [m for m in rig.windows.of(msg.StreamEnd) if m.id == "stop-1"]
    assert answered(rig, "after"), "the message after the refused one was handled"
    assert not answered(rig, "never"), "the window's shutdown ended the loop"


async def test_a_shutdown_already_requested_handles_nothing_more(rig: HostRig) -> None:
    assert rig.orchestrator is not None
    stopping = asyncio.Event()
    stopping.set()
    script(rig, from_(Standing.WINDOW, info_request("late")))

    await rig.orchestrator.serve(stopping)

    assert not answered(rig, "late")


async def test_a_message_in_flight_when_shutdown_arrives_is_let_finish(
    rig: HostRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert rig.orchestrator is not None
    release = asyncio.Event()
    finished: list[str] = []
    original = rig.orchestrator._sessions.route

    async def slow(message: msg.Message) -> None:
        await release.wait()
        await original(message)
        finished.append(message.id)

    monkeypatch.setattr(rig.orchestrator._sessions, "route", slow)
    script(
        rig,
        from_(Standing.WINDOW, msg.Command(id="slow", text="/help")),
        from_(Standing.WINDOW, msg.Shutdown(id="stop")),
    )

    serving = asyncio.create_task(rig.orchestrator.serve(asyncio.Event()))
    await asyncio.sleep(0.05)
    assert not serving.done(), "serve waits for what is in flight"
    release.set()
    await asyncio.wait_for(serving, 5)

    assert finished == ["slow"]


async def test_a_crash_while_routing_becomes_an_error_and_the_next_message_is_handled(
    rig: HostRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert rig.orchestrator is not None
    original = rig.orchestrator._sessions.route

    async def crashing(message: msg.Message) -> None:
        if message.id == "bad":
            raise RuntimeError("pool exploded")
        await original(message)

    monkeypatch.setattr(rig.orchestrator._sessions, "route", crashing)

    await rig.orchestrator.dispatch(msg.Command(id="bad", text="/help", session_id="boot"))
    await rig.orchestrator.dispatch(msg.Command(id="good", text="/help", session_id="boot"))

    [error] = rig.windows.of(msg.Error)
    assert (error.id, error.session_id) == ("bad", "boot")
    assert error.text == "session routing failed: pool exploded"
    assert [m for m in rig.windows.of(msg.CommandResult) if m.id == "good"]


async def test_a_crash_whose_error_cannot_be_sent_is_still_survived(
    rig: HostRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert rig.orchestrator is not None

    async def crashing(message: msg.Message) -> None:
        raise RuntimeError("pool exploded")

    async def unreachable(message: msg.Message) -> None:
        raise ConnectionError("window gone")

    monkeypatch.setattr(rig.orchestrator._sessions, "route", crashing)
    monkeypatch.setattr(rig.windows, "send", unreachable)

    await rig.orchestrator.dispatch(msg.Command(id="bad", text="/help"))


async def test_an_extension_post_to_a_session_not_yet_in_the_pool_makes_it(
    rig: HostRig,
) -> None:
    assert rig.orchestrator is not None
    assert rig.orchestrator.pool.find("fresh") is None

    await rig.orchestrator.post_from_extension(
        ExtensionPost(
            session_id="fresh",
            extension_id="acme.claims",
            text="Hello",
            queued=False,
            from_service="svc",
        )
    )
    await rig.settle()

    runtime = rig.orchestrator.pool.find("fresh")
    assert runtime is not None
    backend: Any = runtime.backend
    assert [text for text, _ in backend.ran] and "Hello" in backend.ran[0][0]


async def test_an_attach_of_a_directory_that_is_not_one_is_refused(
    rig: HostRig, tmp_path: Path
) -> None:
    assert rig.orchestrator is not None

    refused = await rig.orchestrator.attach("", str(tmp_path / "nowhere"))

    assert hasattr(refused, "reason") and refused.reason
