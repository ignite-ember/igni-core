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
