"""``LoopController``: restoring a killed loop, idempotent pause, tool verbs.

``test_session_loop_ops.py`` pins start/advance/cancel/pause/resume and
``test_loop.py`` the tool verbs through the session. This file pins the
rest of the controller directly: what is restored from the store, what is
and is not persisted, how far an implicit cap is extended, and the
tool-facing answers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from igni_core.core.loop.limits import LOOP_DEFAULT_MAX_ITERATIONS, LOOP_HARD_CAP
from igni_core.core.loop.models import LoopState
from igni_core.core.loop.tool_results import LoopToolErrorCode
from igni_core.core.session.loop_ops import LoopController
from igni_core.core.session.schemas import LoopPhase


def _controller(loaded: LoopState | None = None) -> tuple[LoopController, AsyncMock]:
    store = AsyncMock()
    store.load = AsyncMock(return_value=loaded)
    return LoopController(loop_store=store), store


def _state(**kw) -> LoopState:
    base = dict(
        run_id="run-1", prompt="p", iteration_index=3, iterations_remaining=2, cap_explicit=True
    )
    base.update(kw)
    return LoopState(**base)


async def test_an_idle_controller_reads_as_zero_and_none():
    ctrl, _ = _controller()
    assert ctrl.phase is LoopPhase.IDLE
    assert ctrl.pending_loop_prompt is None
    assert ctrl.loop_iteration_index == 0
    assert ctrl.loop_iterations_remaining == 0
    assert ctrl.loop_run_id is None
    assert ctrl.loop_cap_explicit is False
    assert ctrl.paused is False


async def test_a_persisted_loop_comes_back_paused_and_is_not_advanced():
    ctrl, _ = _controller(_state())

    await ctrl.load_persisted_loop_state()

    assert ctrl.phase is LoopPhase.PAUSED
    assert ctrl.loop_paused is True
    assert ctrl.pending_loop_prompt == "p"
    assert ctrl.loop_iteration_index == 3
    assert ctrl.loop_iterations_remaining == 2
    assert ctrl.loop_run_id == "run-1"
    assert await ctrl.advance_loop() is None


async def test_nothing_persisted_leaves_the_controller_idle():
    ctrl, _ = _controller(None)
    await ctrl.load_persisted_loop_state()
    assert ctrl.phase is LoopPhase.IDLE


async def test_loading_twice_changes_nothing():
    ctrl, _ = _controller(_state())
    await ctrl.load_persisted_loop_state()
    await ctrl.load_persisted_loop_state()
    assert ctrl.phase is LoopPhase.PAUSED
    assert ctrl.loop_iteration_index == 3


async def test_a_restored_loop_resumes_with_the_interrupted_iteration():
    ctrl, store = _controller(_state())
    await ctrl.load_persisted_loop_state()

    prompt = await ctrl.resume_loop()

    assert prompt is not None and "p" in prompt
    assert ctrl.phase is LoopPhase.RUNNING
    assert ctrl.loop_iteration_index == 3, "resuming must re-run the iteration, not skip it"
    store.save.assert_awaited_once()


async def test_pausing_a_paused_loop_does_not_write_again():
    ctrl, store = _controller()
    await ctrl.start_loop("p", 5, immediate=True, cap_explicit=True)
    assert await ctrl.pause_loop() is True
    saves = store.save.await_count

    assert await ctrl.pause_loop() is True

    assert store.save.await_count == saves


async def test_pausing_an_idle_controller_says_there_was_nothing_to_pause():
    ctrl, store = _controller()
    assert await ctrl.pause_loop() is False
    store.save.assert_not_awaited()


async def test_the_old_private_persist_name_still_writes_the_state():
    ctrl, store = _controller()
    await ctrl.start_loop("p", 5, immediate=True, cap_explicit=True)
    store.save.reset_mock()

    await ctrl._persist_loop_state()

    store.save.assert_awaited_once_with(ctrl.state)


async def test_persisting_with_no_loop_writes_nothing():
    ctrl, store = _controller()
    await ctrl.persist_loop_state()
    store.save.assert_not_awaited()


def test_the_store_is_the_one_given():
    ctrl, store = _controller()
    assert ctrl.loop_store is store


async def test_cancelling_clears_the_store_and_reports_it():
    ctrl, store = _controller()
    await ctrl.start_loop("p", 5, immediate=True, cap_explicit=False)
    assert await ctrl.cancel_loop() is True
    store.clear.assert_awaited_once()
    assert ctrl.phase is LoopPhase.IDLE


async def test_an_implicit_cap_is_extended_by_the_default_budget():
    ctrl, _ = _controller()
    await ctrl.start_loop("p", 1, immediate=True, cap_explicit=False)

    step = await ctrl.advance_loop()

    assert step.auto_extended is True
    assert step.iteration == 2
    assert step.remaining == LOOP_DEFAULT_MAX_ITERATIONS - 1


async def test_the_extension_shrinks_near_the_hard_cap():
    ctrl, _ = _controller()
    await ctrl.start_loop("p", 1, immediate=True, cap_explicit=False)
    ctrl.state.iteration_index = LOOP_HARD_CAP - 3
    ctrl.state.iterations_remaining = 0

    step = await ctrl.advance_loop()

    assert step.iteration == LOOP_HARD_CAP - 2
    assert step.remaining == 2
    assert step.auto_extended is True


async def test_an_explicit_cap_tells_the_model_the_total_and_an_implicit_one_does_not():
    explicit, _ = _controller()
    await explicit.start_loop("p", 3, immediate=False, cap_explicit=True)
    implicit, _ = _controller()
    await implicit.start_loop("p", 3, immediate=False, cap_explicit=False)

    a = await explicit.advance_loop()
    b = await implicit.advance_loop()

    assert a.prompt != b.prompt
    assert "3" in a.prompt
    assert a.display_prompt == b.display_prompt == "p"


# ── tool verbs ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("prompt", "max_iter", "code"),
    [
        ("", 3, LoopToolErrorCode.EMPTY_PROMPT),
        ("   \n", 3, LoopToolErrorCode.EMPTY_PROMPT),
        (None, 3, LoopToolErrorCode.EMPTY_PROMPT),
        ("go", 0, LoopToolErrorCode.NON_POSITIVE_MAX),
        ("go", -4, LoopToolErrorCode.NON_POSITIVE_MAX),
        ("go", LOOP_HARD_CAP + 1, LoopToolErrorCode.EXCEEDS_HARD_CAP),
    ],
)
async def test_starting_from_the_tool_refuses_what_it_cannot_run(prompt, max_iter, code):
    ctrl, store = _controller()

    result = await ctrl.start_loop_from_tool(prompt, max_iter)

    assert result.ok is False
    assert result.code is code
    assert ctrl.phase is LoopPhase.IDLE
    store.save.assert_not_awaited()


async def test_starting_from_the_tool_at_exactly_the_hard_cap_is_allowed():
    ctrl, _ = _controller()
    result = await ctrl.start_loop_from_tool("go", LOOP_HARD_CAP)
    assert result.ok is True
    assert ctrl.loop_iterations_remaining == LOOP_HARD_CAP


async def test_starting_from_the_tool_arms_a_deferred_explicit_loop_with_a_stripped_prompt():
    ctrl, _ = _controller()

    result = await ctrl.start_loop_from_tool("  go  ", 4)

    assert result.ok is True
    assert ctrl.pending_loop_prompt == "go"
    assert ctrl.loop_iteration_index == 0
    assert ctrl.loop_iterations_remaining == 4
    assert ctrl.loop_cap_explicit is True


async def test_starting_from_the_tool_refuses_a_second_loop():
    ctrl, _ = _controller()
    await ctrl.start_loop_from_tool("first", 4)

    result = await ctrl.start_loop_from_tool("second", 4)

    assert result.code is LoopToolErrorCode.ALREADY_ACTIVE
    assert ctrl.pending_loop_prompt == "first"


async def test_stopping_from_the_tool_reports_the_iterations_done():
    ctrl, _ = _controller()
    await ctrl.start_loop("p", 5, immediate=True, cap_explicit=True)
    await ctrl.advance_loop()

    result = await ctrl.stop_loop_from_tool()

    assert result.ok is True
    assert result.iterations_done == 2
    assert ctrl.phase is LoopPhase.IDLE


async def test_stopping_an_idle_loop_from_the_tool_changes_nothing():
    ctrl, store = _controller()
    result = await ctrl.stop_loop_from_tool()
    assert result.ok is True
    store.clear.assert_not_awaited()


async def test_the_announced_total_goes_to_the_progress_store_under_the_run_id():
    ctrl, _ = _controller()
    run_id = await ctrl.start_loop("p", 5, immediate=True, cap_explicit=False)
    progress = AsyncMock()

    result = await ctrl.set_announced_total_from_tool(7, progress)

    assert result.ok is True
    progress.set_announced_total.assert_awaited_once_with(run_id, 7)


@pytest.mark.parametrize("total", [0, -1])
async def test_a_non_positive_announced_total_is_refused_and_not_stored(total):
    ctrl, _ = _controller()
    await ctrl.start_loop("p", 5, immediate=True, cap_explicit=False)
    progress = AsyncMock()

    result = await ctrl.set_announced_total_from_tool(total, progress)

    assert result.code is LoopToolErrorCode.NON_POSITIVE_TOTAL
    progress.set_announced_total.assert_not_awaited()


async def test_an_announced_total_needs_a_loop_with_a_run_id():
    progress = AsyncMock()
    idle, _ = _controller()
    assert (await idle.set_announced_total_from_tool(3, progress)).code is (
        LoopToolErrorCode.NO_ACTIVE_LOOP
    )
    blank, _ = _controller(_state(run_id=""))
    await blank.load_persisted_loop_state()
    assert (await blank.set_announced_total_from_tool(3, progress)).code is (
        LoopToolErrorCode.NO_ACTIVE_LOOP
    )
    progress.set_announced_total.assert_not_awaited()


async def test_resuming_from_the_tool_covers_idle_running_and_paused():
    ctrl, _ = _controller()
    assert (await ctrl.resume_loop_from_tool()).code is LoopToolErrorCode.NO_LOOP_TO_RESUME

    await ctrl.start_loop("p", 5, immediate=True, cap_explicit=True)
    assert (await ctrl.resume_loop_from_tool()).code is LoopToolErrorCode.NOT_PAUSED

    await ctrl.pause_loop()
    result = await ctrl.resume_loop_from_tool()
    assert result.ok is True
    assert result.iterations_done == 1
    assert ctrl.phase is LoopPhase.RUNNING


async def test_the_status_tool_reports_idle_then_the_active_snapshot():
    ctrl, _ = _controller()
    assert ctrl.loop_status_from_tool().ok is True
    idle_message = ctrl.loop_status_from_tool().message

    await ctrl.start_loop("watch the build", 5, immediate=True, cap_explicit=True)
    active = ctrl.loop_status_from_tool()

    assert active.message != idle_message
    assert active.iteration_index == 1
    assert active.iterations_remaining == 4
    assert active.prompt == "watch the build"
