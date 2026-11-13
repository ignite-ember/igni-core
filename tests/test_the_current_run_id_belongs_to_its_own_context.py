"""``CURRENT_RUN_ID``: a run's id is visible to that run's work only."""

from __future__ import annotations

import asyncio
import threading

from igni_core.core.run_context import CURRENT_RUN_ID


def test_outside_a_run_there_is_no_id():
    assert CURRENT_RUN_ID.get() == ""


async def test_concurrent_runs_each_keep_their_own_id():
    async def run(run_id: str) -> str:
        token = CURRENT_RUN_ID.set(run_id)
        try:
            await asyncio.sleep(0)  # let the other run set its own
            await asyncio.sleep(0)
            return CURRENT_RUN_ID.get()
        finally:
            CURRENT_RUN_ID.reset(token)

    seen = await asyncio.gather(run("a"), run("b"), run("c"))
    assert seen == ["a", "b", "c"]
    assert CURRENT_RUN_ID.get() == ""


def test_a_thread_the_run_did_not_start_carries_no_id():
    token = CURRENT_RUN_ID.set("run-1")
    try:
        seen: list[str] = []
        thread = threading.Thread(target=lambda: seen.append(CURRENT_RUN_ID.get()))
        thread.start()
        thread.join()
    finally:
        CURRENT_RUN_ID.reset(token)
    assert seen == [""]
    assert CURRENT_RUN_ID.get() == ""
