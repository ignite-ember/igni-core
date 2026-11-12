"""``AgentRefresh`` at its edges: a closed loop, another thread, a failing rebuild."""

from __future__ import annotations

import asyncio

from igni_core.backend.agent_refresh import AgentRefresh


def test_a_request_on_a_closed_loop_does_nothing():
    loop = asyncio.new_event_loop()
    loop.close()
    rebuilds: list[int] = []

    AgentRefresh(loop=loop, refresh=lambda: rebuilds.append(1)).request()

    assert rebuilds == []


async def test_a_request_from_another_thread_rebuilds_once_on_the_loop():
    loop = asyncio.get_running_loop()
    rebuilds: list[bool] = []
    refresh = AgentRefresh(
        loop=loop, refresh=lambda: rebuilds.append(asyncio.get_running_loop() is loop)
    )

    await loop.run_in_executor(None, refresh.request)
    await asyncio.sleep(0.05)

    assert rebuilds == [True], "one rebuild, run on the loop's own thread"


async def test_a_failing_rebuild_is_logged_and_the_next_request_still_rebuilds(caplog):
    calls: list[int] = []

    def refresh() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    agents = AgentRefresh(loop=asyncio.get_running_loop(), refresh=refresh)
    agents.request()
    await asyncio.sleep(0)
    agents.request()
    await asyncio.sleep(0)

    assert len(calls) == 2
    assert "rebuilding the agents after an extension change failed" in caplog.text


class _LoopThatRefusesOnce:
    """A loop whose first ``call_soon_threadsafe`` fails as a closing loop's
    does, after ``is_closed`` said it was open; later calls are kept."""

    def __init__(self) -> None:
        self.scheduled: list[object] = []
        self._refused = False

    def is_closed(self) -> bool:
        return False

    def call_soon_threadsafe(self, callback: object) -> None:
        if not self._refused:
            self._refused = True
            raise RuntimeError("Event loop is closed")
        self.scheduled.append(callback)


def test_a_request_the_loop_refused_does_not_block_the_next_one():
    loop = _LoopThatRefusesOnce()
    refresh = AgentRefresh(loop=loop, refresh=lambda: None)  # type: ignore[arg-type]

    refresh.request()
    refresh.request()

    assert len(loop.scheduled) == 1, "the second request is scheduled"
