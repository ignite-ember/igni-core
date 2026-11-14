"""``HookConnections``: the processes connected, and the answers they owe.

A frame goes out on the socket its extension connected on, and its answer is
awaited on a future the hook id names. Only the extension asked can answer;
silence, a process that goes away and a process that never connects are
failures that say which, and leave nothing behind.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from igni_core.extensions.host.hook_exchange import HookConnections, HookFailure
from igni_core.extensions.host.wire import HookAnswer, Outcome


def answer(hook: str, outcome: Outcome = Outcome.ALLOW) -> HookAnswer:
    return HookAnswer(hook=hook, outcome=outcome)


class Socket:
    """A process's socket: the frames put on it."""

    def __init__(self) -> None:
        self.frames: list[str] = []

    def __call__(self, frame: str) -> None:
        self.frames.append(frame)


class TestWhoIsConnected:
    async def test_a_connected_process_is_sent_frames_and_one_that_is_not_is_not(self) -> None:
        connections, socket = HookConnections(), Socket()
        assert not connections.connected("acme.one")
        assert connections.send("acme.one", "frame") is False
        connections.connect("acme.one", socket)
        assert connections.connected("acme.one")
        assert connections.send("acme.one", "frame") is True
        assert socket.frames == ["frame"]
        assert connections.send("acme.two", "frame") is False

    async def test_a_disconnect_without_a_socket_removes_whatever_is_connected(self) -> None:
        connections = HookConnections()
        connections.connect("acme.one", Socket())
        connections.disconnect("acme.one")
        assert not connections.connected("acme.one")
        assert connections.send("acme.one", "frame") is False

    async def test_a_disconnect_of_a_socket_that_was_replaced_is_ignored(self) -> None:
        connections, old, new = HookConnections(), Socket(), Socket()
        connections.connect("acme.one", old)
        connections.connect("acme.one", new)
        connections.disconnect("acme.one", old)
        assert connections.connected("acme.one")
        connections.send("acme.one", "frame")
        assert new.frames == ["frame"] and old.frames == []
        connections.disconnect("acme.one", new)
        assert not connections.connected("acme.one")

    async def test_hook_ids_are_never_repeated(self) -> None:
        connections = HookConnections()
        ids = [connections.next_hook_id() for _ in range(5)]
        assert ids == ["h-1", "h-2", "h-3", "h-4", "h-5"]


class TestAnswers:
    async def test_only_the_extension_asked_can_answer_a_hook(self) -> None:
        connections = HookConnections()
        connections.connect("acme.one", Socket())
        connections.connect("acme.two", Socket())
        asking = asyncio.create_task(
            connections.await_answer("acme.one", "h-1", "frame", timeout=5)
        )
        await asyncio.sleep(0)
        assert connections.answered("acme.two", answer("h-1", Outcome.REJECT)) is False
        assert not asking.done()
        given = answer("h-1")
        assert connections.answered("acme.one", given) is True
        assert await asking == given

    async def test_an_answer_to_nothing_asked_answers_nothing(self) -> None:
        connections = HookConnections()
        connections.connect("acme.one", Socket())
        assert connections.answered("acme.one", answer("h-99")) is False

    async def test_the_first_answer_stands(self) -> None:
        connections = HookConnections()
        connections.connect("acme.one", Socket())
        asking = asyncio.create_task(
            connections.await_answer("acme.one", "h-1", "frame", timeout=5)
        )
        await asyncio.sleep(0)
        first, second = answer("h-1"), answer("h-1", Outcome.REJECT)
        assert connections.answered("acme.one", first) is True
        assert connections.answered("acme.one", second) is True
        assert await asking == first

    async def test_an_answer_that_comes_after_the_wait_ended_answers_nothing(self) -> None:
        connections = HookConnections()
        connections.connect("acme.one", Socket())
        with pytest.raises(HookFailure):
            await connections.await_answer("acme.one", "h-1", "frame", timeout=0.02)
        assert connections.answered("acme.one", answer("h-1")) is False

    async def test_the_frame_goes_out_on_the_socket_of_the_one_asked(self) -> None:
        connections, one, two = HookConnections(), Socket(), Socket()
        connections.connect("acme.one", one)
        connections.connect("acme.two", two)
        with pytest.raises(HookFailure):
            await connections.await_answer("acme.one", "h-1", "the frame", timeout=0.02)
        assert one.frames == ["the frame"] and two.frames == []


class TestSilence:
    async def test_no_answer_in_time_is_a_silence_that_says_how_long(self) -> None:
        connections = HookConnections()
        connections.connect("acme.one", Socket())
        with pytest.raises(HookFailure) as caught:
            await connections.await_answer("acme.one", "h-1", "frame", timeout=0.05)
        assert str(caught.value) == "no answer within 0.05s"
        assert caught.value.unanswered is True

    async def test_a_process_that_goes_away_fails_what_it_owed_at_once(self) -> None:
        connections = HookConnections()
        connections.connect("acme.one", Socket())
        asking = asyncio.create_task(
            connections.await_answer("acme.one", "h-1", "frame", timeout=30)
        )
        await asyncio.sleep(0)
        connections.disconnect("acme.one")
        with pytest.raises(HookFailure) as caught:
            await asking
        assert str(caught.value) == "its process went away"
        assert caught.value.unanswered is True

    async def test_a_process_going_away_does_not_fail_another_extensions_wait(self) -> None:
        connections = HookConnections()
        connections.connect("acme.one", Socket())
        connections.connect("acme.two", Socket())
        asking = asyncio.create_task(
            connections.await_answer("acme.two", "h-1", "frame", timeout=5)
        )
        await asyncio.sleep(0)
        connections.disconnect("acme.one")
        await asyncio.sleep(0)
        assert not asking.done()
        connections.answered("acme.two", answer("h-1"))
        assert (await asking).outcome is Outcome.ALLOW

    async def test_a_stale_disconnect_does_not_fail_what_the_new_process_owes(self) -> None:
        connections, old = HookConnections(), Socket()
        connections.connect("acme.one", old)
        connections.connect("acme.one", Socket())
        asking = asyncio.create_task(
            connections.await_answer("acme.one", "h-1", "frame", timeout=5)
        )
        await asyncio.sleep(0)
        connections.disconnect("acme.one", old)
        await asyncio.sleep(0)
        assert not asking.done()
        connections.answered("acme.one", answer("h-1"))
        assert (await asking).hook == "h-1"


class TestWaitingForTheProcess:
    async def test_a_process_that_never_connects_is_not_connected_after_the_wait(self) -> None:
        connections = HookConnections()
        with pytest.raises(HookFailure) as caught:
            await connections.await_answer(
                "acme.one", "h-1", "frame", timeout=5, connect_timeout=0.02
            )
        assert str(caught.value) == "its process is not connected"
        assert caught.value.unanswered is True

    async def test_a_process_that_connects_during_the_wait_is_asked(self) -> None:
        connections, socket = HookConnections(), Socket()
        asking = asyncio.create_task(
            connections.await_answer("acme.one", "h-1", "frame", timeout=5, connect_timeout=5)
        )
        await asyncio.sleep(0.01)
        connections.connect("acme.one", socket)
        await asyncio.sleep(0.01)
        assert socket.frames == ["frame"]
        connections.answered("acme.one", answer("h-1"))
        assert (await asking).hook == "h-1"

    async def test_the_wait_for_a_connection_is_never_longer_than_the_answer_deadline(
        self,
    ) -> None:
        connections = HookConnections()
        started = time.monotonic()
        with pytest.raises(HookFailure):
            await connections.await_answer(
                "acme.one", "h-1", "frame", timeout=0.05, connect_timeout=2
            )
        assert time.monotonic() - started < 1

    async def test_without_a_connect_timeout_the_connection_is_waited_for_as_long_as_the_answer(
        self,
    ) -> None:
        connections, socket = HookConnections(), Socket()
        asking = asyncio.create_task(
            connections.await_answer("acme.one", "h-1", "frame", timeout=5)
        )
        await asyncio.sleep(0.02)
        connections.connect("acme.one", socket)
        await asyncio.sleep(0.02)
        assert socket.frames == ["frame"]
        connections.answered("acme.one", answer("h-1"))
        await asking

    async def test_time_spent_waiting_for_the_connection_comes_off_the_answer_deadline(
        self,
    ) -> None:
        connections, socket = HookConnections(), Socket()
        started = time.monotonic()
        asking = asyncio.create_task(
            connections.await_answer("acme.one", "h-1", "frame", timeout=0.3, connect_timeout=5)
        )
        await asyncio.sleep(0.2)
        connections.connect("acme.one", socket)
        with pytest.raises(HookFailure):
            await asking
        assert time.monotonic() - started < 0.45
