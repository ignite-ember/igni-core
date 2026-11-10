"""``cancel_run`` stops the run, the team's own included, and a resumed one too.

Two gaps let a cancelled run go on:

* the multiplexer drains the team's Agno stream in a task of its own, and
  that task was never cancelled — the person's Escape ended what igni
  showed while the team went on running tools;
* a run resumed after its approvals (``acontinue_run``) was never the
  session's run, so ``cancel_run`` found nothing to cancel and
  ``get_processing`` said the session was idle.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from igni_core.backend.hitl_stream_mux import HITLStreamMultiplexer
from igni_core.backend.hitl_tracer import HITLTracer
from igni_core.backend.pause_handler import PauseHandler
from igni_core.backend.pending_requirements_store import PendingRequirementsStore
from igni_core.backend.run_controller import RunController
from igni_core.backend.schemas_run import RunPhase
from igni_core.core.config.permission_eval import PermissionEvaluator
from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator
from igni_core.protocol import messages as msg


async def test_cancelling_the_stream_cancels_the_team_s_run() -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def team_stream() -> AsyncIterator[Any]:
        started.set()
        try:
            await asyncio.sleep(3600)
            yield None
        except asyncio.CancelledError:
            stopped.set()
            raise

    store = PendingRequirementsStore()
    session = SimpleNamespace(
        sub_agent_hitl=SubAgentHITLCoordinator(), broadcast_bus=None, session_id="s-1"
    )
    mux = HITLStreamMultiplexer(
        session=session,  # type: ignore[arg-type]
        store=store,
        pause_handler=PauseHandler(store=store, evaluator=PermissionEvaluator()),
        tracer=HITLTracer(),
    )

    async def consume() -> None:
        async for _ in mux.stream(team_stream()):
            pass

    consumer = asyncio.create_task(consume())
    await started.wait()
    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)
    await asyncio.wait_for(stopped.wait(), 2)


def _controller() -> RunController:
    controller = RunController.__new__(RunController)
    controller._phase = RunPhase.idle
    controller._pending_id_for_cancel = None
    controller._current_run_task = None
    controller._supervisor = SimpleNamespace(cancel_foreground=lambda: False)
    return controller


async def test_a_resumed_run_is_the_session_s_run_and_cancel_reaches_it() -> None:
    controller = _controller()
    resumed = asyncio.Event()

    async def resumed_run() -> AsyncIterator[msg.Message]:
        yield msg.ContentDelta(text="resumed")
        resumed.set()
        await asyncio.sleep(3600)
        yield msg.ContentDelta(text="never")

    got: list[msg.Message] = []

    async def consume() -> None:
        async for proto in controller.continue_run(resumed_run()):
            got.append(proto)

    consumer = asyncio.create_task(consume())
    await resumed.wait()
    assert controller._phase.is_active(), "a resumed run is the session's run"

    controller.cancel_run()
    await asyncio.wait_for(consumer, 2)

    assert [type(m) for m in got] == [msg.ContentDelta, msg.Info]
    assert got[-1].text == "Run cancelled by user."
    assert not controller._phase.is_active()
    assert controller._current_run_task is None


async def test_a_sub_agent_s_resume_inside_a_running_turn_passes_through() -> None:
    controller = _controller()
    parent = asyncio.current_task()
    controller._current_run_task = parent

    async def resumed_run() -> AsyncIterator[msg.Message]:
        yield msg.ContentDelta(text="sub-agent resumed")

    got = [m async for m in controller.continue_run(resumed_run())]

    assert len(got) == 1
    assert controller._current_run_task is parent


async def test_a_scheduled_task_run_now_is_the_session_s_run() -> None:
    """``execute_scheduled_task`` ran the team beside the person's turn,
    on the same session, unseen by ``get_processing`` and out of
    ``cancel_run``'s reach."""
    import pytest

    from igni_core.backend.run_controller import RunCancelledError

    controller = _controller()
    controller._run_lock = asyncio.Lock()
    started = asyncio.Event()

    async def work() -> str:
        started.set()
        await asyncio.sleep(3600)
        return "never"

    running = asyncio.create_task(controller.run_unstreamed(work))
    await started.wait()
    assert controller._phase.is_active()

    controller.cancel_run()
    with pytest.raises(RunCancelledError, match="cancelled by user"):
        await asyncio.wait_for(running, 2)
    assert not controller._phase.is_active()


async def test_a_scheduled_task_waits_for_the_turn_already_running() -> None:
    controller = _controller()
    controller._run_lock = asyncio.Lock()
    order: list[str] = []

    async def work() -> str:
        order.append("task")
        return "done"

    async with controller._run_lock:
        waiting = asyncio.create_task(controller.run_unstreamed(work))
        await asyncio.sleep(0.05)
        order.append("turn ends")

    assert await waiting == "done"
    assert order == ["turn ends", "task"]
