"""What each loop tool outcome says and carries.

The messages are what the model reads back from ``loop_start``,
``loop_stop``, ``loop_status``, ``loop_set_total`` and ``loop_resume``;
they are pinned byte for byte, with the code and structured fields a
non-prose consumer branches on.
"""

from __future__ import annotations

import pytest

from igni_core.core.loop import LoopToolErrorCode, LoopToolResult

OK, ERR = True, False
C = LoopToolErrorCode


def test_the_codes_have_stable_wire_values():
    assert {c.name: c.value for c in LoopToolErrorCode} == {
        "OK": "ok",
        "EMPTY_PROMPT": "empty_prompt",
        "NON_POSITIVE_MAX": "non_positive_max",
        "EXCEEDS_HARD_CAP": "exceeds_hard_cap",
        "ALREADY_ACTIVE": "already_active",
        "NO_ACTIVE_LOOP": "no_active_loop",
        "NON_POSITIVE_TOTAL": "non_positive_total",
        "NOT_PAUSED": "not_paused",
        "NO_LOOP_TO_RESUME": "no_loop_to_resume",
    }


CASES = [
    (
        LoopToolResult.armed(5),
        OK,
        C.OK,
        "Loop armed. Will re-fire this prompt up to 5 more times. "
        "First iteration runs as the next turn.",
        {"max_iterations": 5},
    ),
    (
        LoopToolResult.stopped(1),
        OK,
        C.OK,
        "Loop stopped after 1 iteration.",
        {"iterations_done": 1},
    ),
    (
        LoopToolResult.stopped(0),
        OK,
        C.OK,
        "Loop stopped after 0 iterations.",
        {"iterations_done": 0},
    ),
    (
        LoopToolResult.stopped(2),
        OK,
        C.OK,
        "Loop stopped after 2 iterations.",
        {"iterations_done": 2},
    ),
    (
        LoopToolResult.idle_stop(),
        OK,
        C.NO_ACTIVE_LOOP,
        "No loop is active. Nothing to stop.",
        {},
    ),
    (LoopToolResult.status_idle(), OK, C.NO_ACTIVE_LOOP, "No loop is active.", {}),
    (
        LoopToolResult.status_active(iteration_index=3, iterations_remaining=7, prompt="do it"),
        OK,
        C.OK,
        "Loop active: iteration 3 done, 7 remaining. Prompt: 'do it'",
        {"iteration_index": 3, "iterations_remaining": 7, "prompt": "do it"},
    ),
    (
        LoopToolResult.total_announced(12),
        OK,
        C.OK,
        "Announced loop total: 12 iterations.",
        {"announced_total": 12},
    ),
    (
        LoopToolResult.resumed(4),
        OK,
        C.OK,
        "Loop unpaused (4 done so far). Next iteration fires after this turn.",
        {"iterations_done": 4},
    ),
    (
        LoopToolResult.resume_noop_running(),
        OK,
        C.NOT_PAUSED,
        "Loop is already running — no resume needed.",
        {},
    ),
    (LoopToolResult.resume_race_no_prompt(), OK, C.NOT_PAUSED, "Loop is already running.", {}),
    (
        LoopToolResult.error_empty_prompt(),
        ERR,
        C.EMPTY_PROMPT,
        "ERROR: loop_start needs a non-empty prompt.",
        {},
    ),
    (
        LoopToolResult.error_non_positive_max(),
        ERR,
        C.NON_POSITIVE_MAX,
        "ERROR: max_iterations must be positive.",
        {},
    ),
    (
        LoopToolResult.error_exceeds_hard_cap(201, 200),
        ERR,
        C.EXCEEDS_HARD_CAP,
        "ERROR: max_iterations=201 exceeds the hard cap of 200. Pick a smaller number.",
        {"max_iterations": 201},
    ),
    (
        LoopToolResult.error_already_active(iteration_index=2, iterations_remaining=8),
        ERR,
        C.ALREADY_ACTIVE,
        "ERROR: a loop is already active (2 done, 8 remaining). "
        "Call loop_stop() first if you want to start a new one.",
        {"iteration_index": 2, "iterations_remaining": 8},
    ),
    (
        LoopToolResult.error_no_active_loop(),
        ERR,
        C.NO_ACTIVE_LOOP,
        "ERROR: no loop is active — call loop_start() first.",
        {},
    ),
    (
        LoopToolResult.error_non_positive_total(),
        ERR,
        C.NON_POSITIVE_TOTAL,
        "ERROR: total must be a positive integer.",
        {},
    ),
    (
        LoopToolResult.error_no_loop_to_resume(),
        ERR,
        C.NO_LOOP_TO_RESUME,
        "ERROR: no loop to resume.",
        {},
    ),
]

FIELDS = (
    "iterations_done",
    "iterations_remaining",
    "iteration_index",
    "max_iterations",
    "announced_total",
    "prompt",
)


@pytest.mark.parametrize(("result", "ok", "code", "message", "fields"), CASES)
def test_each_outcome_says_and_carries_exactly_this(result, ok, code, message, fields):
    assert result.ok is ok
    assert result.is_ok is ok
    assert result.code is code
    assert result.message == message
    assert result.render() == message
    for name in FIELDS:
        assert getattr(result, name) == fields.get(name), name


def test_a_prompt_with_quotes_newlines_and_unicode_is_shown_escaped_and_whole():
    prompt = "say \"hi\"\nand 'bye' — 日本語 " + "x" * 5000
    result = LoopToolResult.status_active(iteration_index=1, iterations_remaining=2, prompt=prompt)
    assert result.render().endswith(f"Prompt: {prompt!r}")
    assert result.prompt == prompt
    assert "\n" not in result.render()


def test_the_no_op_outcomes_are_ok_with_a_non_ok_code():
    for result in (
        LoopToolResult.idle_stop(),
        LoopToolResult.status_idle(),
        LoopToolResult.resume_noop_running(),
        LoopToolResult.resume_race_no_prompt(),
    ):
        assert result.ok is True
        assert result.code is not LoopToolErrorCode.OK
