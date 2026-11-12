"""``/loop``'s chat cards and the panel's status snapshot say what the person reads.

The snapshot is built from a session's loop state and, for a loop with a run id,
from the agent's announced total read out of the real progress store.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from igni_core.backend.schemas_loop import (
    LoopAlreadyRunningView,
    LoopResumedView,
    LoopStartedView,
    LoopStatusSnapshot,
    LoopStatusView,
    LoopStoppedView,
    LoopUsageView,
)
from igni_core.core.loop.store import LoopProgressStore
from igni_core.protocol.messages import CommandAction, CommandResultKind


@pytest.fixture
def store(tmp_path):
    return LoopProgressStore(db_path=tmp_path / "state.db")


def _session(store, **loop: object) -> SimpleNamespace:
    state = {
        "pending_loop_prompt": None,
        "paused": False,
        "loop_run_id": None,
        "loop_iteration_index": 0,
        "loop_iterations_remaining": 0,
        "loop_cap_explicit": False,
    }
    state.update(loop)
    return SimpleNamespace(loop=SimpleNamespace(**state), loop_progress_store=store)


async def test_an_idle_session_has_no_loop_and_asks_the_store_nothing(store) -> None:
    class Untouched:
        async def get_announced_total(self, run_id: str) -> int | None:
            raise AssertionError("no run id, so no lookup")

    snap = await LoopStatusSnapshot.from_session(_session(Untouched()))

    assert snap.model_dump() == {
        "active": False,
        "paused": False,
        "prompt": "",
        "iteration_index": 0,
        "iterations_remaining": 0,
        "cap_explicit": False,
        "announced_total": None,
    }


async def test_a_running_loop_reports_its_prompt_counters_and_the_announced_total(store) -> None:
    await store.set("run-1", LoopProgressStore.ANNOUNCED_TOTAL_KEY, "12")
    session = _session(
        store,
        pending_loop_prompt="check every section",
        loop_run_id="run-1",
        loop_iteration_index=3,
        loop_iterations_remaining=9,
        loop_cap_explicit=True,
    )

    snap = await LoopStatusSnapshot.from_session(session)

    assert snap.model_dump() == {
        "active": True,
        "paused": False,
        "prompt": "check every section",
        "iteration_index": 3,
        "iterations_remaining": 9,
        "cap_explicit": True,
        "announced_total": 12,
    }


async def test_a_loop_whose_agent_has_announced_nothing_has_no_total(store) -> None:
    await store.set("run-1", "some_other_key", "5")
    session = _session(store, pending_loop_prompt="p", loop_run_id="run-1")

    assert (await LoopStatusSnapshot.from_session(session)).announced_total is None


async def test_the_total_of_another_run_is_not_this_runs(store) -> None:
    await store.set("run-2", LoopProgressStore.ANNOUNCED_TOTAL_KEY, "40")
    session = _session(store, pending_loop_prompt="p", loop_run_id="run-1")

    assert (await LoopStatusSnapshot.from_session(session)).announced_total is None


async def test_a_paused_loop_is_still_active_and_says_it_is_paused(store) -> None:
    session = _session(
        store, pending_loop_prompt="ünï ⚡ " + "x" * 5000, paused=True, loop_iteration_index=30
    )

    snap = await LoopStatusSnapshot.from_session(session)

    assert (snap.active, snap.paused) == (True, True)
    assert snap.prompt == "ünï ⚡ " + "x" * 5000


def test_the_usage_card_lists_every_way_to_drive_a_loop() -> None:
    result = LoopUsageView.to_command_result()

    assert result.kind is CommandResultKind.INFO
    assert result.content == (
        "No loop is running.\n\n"
        "Usage:\n"
        "  /loop <prompt>          start (default cap: 30 iterations)\n"
        "  /loop <N> <prompt>      start with explicit cap N\n"
        "  /loop stop              cancel the active loop\n"
        "  /loop resume            re-fire the interrupted iteration after a restart"
    )


def test_the_status_card_shows_the_prompt_quoted_on_one_line() -> None:
    result = LoopStatusView(
        iteration_index=2, iterations_remaining=0, pending_prompt="a\nb 'c'"
    ).to_command_result()

    assert result.kind is CommandResultKind.INFO
    assert result.content == "Loop active: 2 done, 0 remaining.\nPrompt: \"a\\nb 'c'\""


def test_starting_on_a_live_loop_is_an_error_that_names_the_way_out() -> None:
    result = LoopAlreadyRunningView(iteration_index=4, iterations_remaining=26).to_command_result()

    assert result.kind is CommandResultKind.ERROR
    assert result.content == (
        "A loop is already running (4 done, 26 remaining). "
        "Run `/loop stop` first, then start a new one."
    )


@pytest.mark.parametrize(
    ("done", "text"),
    [
        (0, "Loop stopped after 0 iterations."),
        (1, "Loop stopped after 1 iteration."),
        (2, "Loop stopped after 2 iterations."),
    ],
)
def test_the_stop_line_agrees_in_number(done: int, text: str) -> None:
    result = LoopStoppedView(iterations_done=done).to_command_result()

    assert (result.kind, result.content) == (CommandResultKind.INFO, text)


def test_a_started_loop_sends_the_wrapper_to_the_agent_and_shows_the_person_their_prompt() -> None:
    result = LoopStartedView(wrapped_prompt="WRAPPED", display_prompt="fix it").to_command_result()

    assert result.content == "WRAPPED"
    assert result.display_content == "fix it"
    assert result.action is CommandAction.RUN_PROMPT


def test_a_resumed_loop_re_fires_the_saved_prompt() -> None:
    result = LoopResumedView(prompt="saved").to_command_result()

    assert result.content == "saved"
    assert result.display_content == ""
    assert result.action is CommandAction.RUN_PROMPT


def test_every_field_of_the_snapshot_is_documented_for_the_sdks() -> None:
    props = LoopStatusSnapshot.model_json_schema()["properties"]

    assert set(props) == {
        "active",
        "paused",
        "prompt",
        "iteration_index",
        "iterations_remaining",
        "cap_explicit",
        "announced_total",
    }
    assert all(p.get("description") for p in props.values())
