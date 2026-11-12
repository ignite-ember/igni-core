"""``LoopController`` — the ``/loop`` RPCs over a real session loop.

Each RPC reads or moves the session's real loop state (a real
:class:`LoopController` over a real :class:`LoopStore` on a temp database),
so what is pinned is what the panel sees, not that a method was called.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from igni_core.backend.schemas_loop import LoopStatusSnapshot
from igni_core.backend.server_loop import LoopController, LoopStatus
from igni_core.backend.server_scheduler import SchedulerController
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.loop.store import LoopProgressStore, LoopStore
from igni_core.core.session.loop_ops import LoopController as SessionLoop


@pytest.fixture
def session(tmp_path):
    db = tmp_path / "state.db"
    return SimpleNamespace(
        loop=SessionLoop(LoopStore(db_path=db)),
        loop_progress_store=LoopProgressStore(db_path=db),
        project_dir=tmp_path,
    )


@pytest.fixture
def controller(session) -> LoopController:
    return LoopController(session, None, RunGate())  # type: ignore[arg-type]


async def test_with_no_loop_nothing_pops_cancels_or_resumes(controller) -> None:
    assert await controller.pop_pending_iteration() is None
    assert await controller.cancel_pending() is False
    assert await controller.resume() == ""
    assert await controller.pause() is False
    status = await controller.status()
    assert (status.active, status.paused, status.prompt) == (False, False, "")
    assert (status.iteration_index, status.iterations_remaining) == (0, 0)


async def test_a_pumping_loop_pops_an_iteration_and_cancel_clears_it(session, controller) -> None:
    await session.loop.start_loop("watch the build", 3, immediate=False, cap_explicit=True)

    step = await controller.pop_pending_iteration()

    assert step is not None
    assert step.iteration == 1
    assert step.remaining == 2
    assert await controller.cancel_pending() is True
    assert await controller.pop_pending_iteration() is None
    assert (await controller.status()).active is False


async def test_pause_holds_the_counter_and_resume_returns_the_prompt(session, controller) -> None:
    await session.loop.start_loop("watch the build", 5, immediate=True, cap_explicit=True)

    assert await controller.pause() is True

    paused = await controller.status()
    assert (paused.active, paused.paused) == (True, True)
    assert (paused.iteration_index, paused.iterations_remaining) == (1, 4)
    assert await controller.pop_pending_iteration() is None, "a paused loop does not pump"

    prompt = await controller.resume()

    assert "watch the build" in prompt
    resumed = await controller.status()
    assert (resumed.paused, resumed.iteration_index) == (False, 1)
    assert await controller.resume() == "", "already pumping: nothing to resume"


async def test_a_paused_loop_is_not_cancelled_by_cancel_pending(session, controller) -> None:
    await session.loop.start_loop("watch the build", 5, immediate=True, cap_explicit=False)
    await controller.pause()

    assert await controller.cancel_pending() is False

    status = await controller.status()
    assert (status.active, status.paused, status.prompt) == (True, True, "watch the build")


async def test_status_carries_the_total_the_agent_announced(session, controller) -> None:
    run_id = await session.loop.start_loop("sweep", 4, immediate=True, cap_explicit=True)
    await session.loop_progress_store.set_announced_total(run_id, 7)

    status = await controller.status()

    assert isinstance(status, LoopStatusSnapshot)
    assert status.announced_total == 7
    assert status.cap_explicit is True
    assert status.prompt == "sweep"


def test_the_scheduler_is_composed_from_the_same_session_and_gate(session) -> None:
    gate = RunGate()
    settings = object()
    seen: list[str] = []

    async def as_run(work):
        seen.append("as_run")
        return await work()

    loop = LoopController(session, settings, gate, as_run)  # type: ignore[arg-type]

    scheduler = loop.scheduler
    assert isinstance(scheduler, SchedulerController)
    assert loop.scheduler is scheduler
    assert scheduler._session is session
    assert scheduler._settings is settings
    assert scheduler._gate is gate
    assert scheduler._as_run is as_run


def test_the_old_loop_status_name_is_the_same_model() -> None:
    assert LoopStatus is LoopStatusSnapshot
