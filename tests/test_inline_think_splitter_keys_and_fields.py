"""What the inline ``<think>`` splitter keys its state on, and what it copies.

``test_inline_think_splitter.py`` pins the split itself. These pin the
rest of the contract: which messages pass through, how a stream's state
is keyed (message id, else run id, else a shared key), what the split
deltas carry, and that ending a run drops only that run's state.
"""

from __future__ import annotations

from igni_core.backend.inline_think_splitter import InlineThinkSplitter
from igni_core.protocol import messages as msg


def _delta(text: str, **kw) -> msg.ContentDelta:
    return msg.ContentDelta(text=text, **kw)


def _roles(out: list[msg.Message]) -> list[tuple[str, bool]]:
    return [(m.text, m.is_thinking) for m in out]  # type: ignore[attr-defined]


def test_a_message_that_is_not_content_passes_through_untouched() -> None:
    info = msg.Info(text="<think>not content</think>")

    assert InlineThinkSplitter().split(info, "run") == [info]


def test_an_empty_text_delta_gives_nothing_to_show() -> None:
    assert InlineThinkSplitter().split(_delta(""), "run") == []


def test_split_deltas_keep_the_id_and_session_and_restart_the_sequence() -> None:
    original = _delta("<think>why</think>because", id="m-1", session_id="s-1", event_seq=17)

    out = InlineThinkSplitter().split(original, "run")

    assert _roles(out) == [("why", True), ("because", False)]
    for piece in out:
        assert piece.id == "m-1" and piece.session_id == "s-1"
        assert piece.event_seq == 0
        assert piece is not original


def test_an_empty_segment_is_dropped() -> None:
    out = InlineThinkSplitter().split(_delta("<think></think>answer"), "run")

    assert _roles(out) == [("answer", False)]


def test_a_text_that_is_all_inside_a_block_becomes_only_thinking() -> None:
    splitter = InlineThinkSplitter()

    out = splitter.split(_delta("<think>just thinking"), "run")

    assert _roles(out) == [("just thinking", True)]


def test_the_rest_of_a_block_after_a_split_is_a_new_delta_not_the_original() -> None:
    splitter = InlineThinkSplitter()
    splitter.split(_delta("<think>open"), "run")
    tail = _delta("more</think>")

    out = splitter.split(tail, "run")

    # The text minus the closing tag is not the original text, so the original must not leak it.
    assert _roles(out) == [("more", True)]
    assert out[0] is not tail


def test_the_same_message_id_carries_a_block_across_events_whatever_the_run() -> None:
    splitter = InlineThinkSplitter()
    splitter.split(_delta("<think>one", id="m"), "run-a")

    out = splitter.split(_delta(" two</think>done", id="m"), "run-b")

    assert _roles(out) == [(" two", True), ("done", False)]


def test_without_a_message_id_the_run_id_keys_the_state() -> None:
    splitter = InlineThinkSplitter()
    splitter.split(_delta("<think>in run a"), "run-a")

    other = splitter.split(_delta("visible in b"), "run-b")
    same = splitter.split(_delta("still thinking"), "run-a")

    assert _roles(other) == [("visible in b", False)]
    assert _roles(same) == [("still thinking", True)]


def test_without_id_or_run_all_such_content_shares_one_stream() -> None:
    splitter = InlineThinkSplitter()
    splitter.split(_delta("<think>open"), None)

    out = splitter.split(_delta("still open"), None)

    assert _roles(out) == [("still open", True)]


def test_ending_a_run_does_not_clear_state_held_under_a_message_id() -> None:
    splitter = InlineThinkSplitter()
    splitter.split(_delta("<think>cut", id="m-1"), "run-a")

    splitter.end_run("run-a")

    out = splitter.split(_delta("continues", id="m-1"), "run-a")
    assert _roles(out) == [("continues", True)]


def test_ending_a_run_drops_only_that_runs_open_block() -> None:
    splitter = InlineThinkSplitter()
    splitter.split(_delta("<think>a"), "run-a")
    splitter.split(_delta("<think>b"), "run-b")

    splitter.end_run("run-a")

    assert _roles(splitter.split(_delta("x"), "run-a")) == [("x", False)]
    assert _roles(splitter.split(_delta("y"), "run-b")) == [("y", True)]


def test_ending_a_run_nobody_started_is_a_no_op() -> None:
    splitter = InlineThinkSplitter()

    splitter.end_run("never-seen")

    assert _roles(splitter.split(_delta("hello"), "run")) == [("hello", False)]


def test_unicode_survives_the_split() -> None:
    out = InlineThinkSplitter().split(_delta("<think>думаю — 考える</think>ответ"), "run")

    assert _roles(out) == [("думаю — 考える", True), ("ответ", False)]
