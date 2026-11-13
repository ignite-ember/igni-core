"""The loop package's public names, its limits and the state model."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import igni_core.core.loop as loop
from igni_core.core.loop import limits
from igni_core.core.loop.models import LoopState
from igni_core.core.loop.prompt import wrap_iteration_prompt
from igni_core.core.loop.store import LoopProgressStore, LoopStore
from igni_core.core.loop.tool_results import LoopToolErrorCode, LoopToolResult


def test_the_package_exports_exactly_these_names_each_the_submodules_object():
    assert sorted(loop.__all__) == [
        "LOOP_DEFAULT_MAX_ITERATIONS",
        "LOOP_HARD_CAP",
        "LoopProgressStore",
        "LoopState",
        "LoopStore",
        "LoopToolErrorCode",
        "LoopToolResult",
        "wrap_iteration_prompt",
    ]
    assert loop.LoopState is LoopState
    assert loop.LoopStore is LoopStore
    assert loop.LoopProgressStore is LoopProgressStore
    assert loop.LoopToolResult is LoopToolResult
    assert loop.LoopToolErrorCode is LoopToolErrorCode
    assert loop.wrap_iteration_prompt is wrap_iteration_prompt
    assert loop.LOOP_DEFAULT_MAX_ITERATIONS is limits.LOOP_DEFAULT_MAX_ITERATIONS
    assert loop.LOOP_HARD_CAP is limits.LOOP_HARD_CAP


def test_an_implicit_loop_runs_thirty_iterations_a_batch_up_to_a_hard_cap_of_two_hundred():
    assert limits.LOOP_DEFAULT_MAX_ITERATIONS == 30
    assert limits.LOOP_HARD_CAP == 200


class TestLoopState:
    def test_the_cap_is_implicit_unless_said_otherwise(self):
        state = LoopState(run_id="r", prompt="p", iteration_index=1, iterations_remaining=2)
        assert state.cap_explicit is False

    def test_all_five_fields_are_kept(self):
        state = LoopState(
            run_id="r",
            prompt="日本語",
            iteration_index=0,
            iterations_remaining=-1,
            cap_explicit=True,
        )
        assert state.model_dump() == {
            "run_id": "r",
            "prompt": "日本語",
            "iteration_index": 0,
            "iterations_remaining": -1,
            "cap_explicit": True,
        }

    @pytest.mark.parametrize(
        "missing", ["run_id", "prompt", "iteration_index", "iterations_remaining"]
    )
    def test_a_missing_required_field_is_refused(self, missing):
        fields = {"run_id": "r", "prompt": "p", "iteration_index": 1, "iterations_remaining": 2}
        del fields[missing]
        with pytest.raises(ValidationError):
            LoopState(**fields)

    def test_a_counter_that_is_not_a_number_is_refused(self):
        with pytest.raises(ValidationError):
            LoopState(run_id="r", prompt="p", iteration_index="soon", iterations_remaining=1)

    def test_an_empty_prompt_is_the_models_to_accept(self):
        assert (
            LoopState(run_id="r", prompt="", iteration_index=1, iterations_remaining=1).prompt == ""
        )
