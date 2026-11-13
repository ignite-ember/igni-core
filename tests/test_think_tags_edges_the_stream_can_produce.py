"""Edges of ``split_inline_think_tags`` beside ``test_text_tags.py``: the
``<thinking>`` spelling, literal ``<``, nested opens, whitespace the tag
does not own, a dangling close from visible mode, and purity."""

from __future__ import annotations

import pytest

from igni_core.core.text_tags import INITIAL_STATE, ThinkParseState, split_inline_think_tags


def test_the_thinking_spelling_is_a_block_too_and_mixes_with_think():
    segments, state = split_inline_think_tags(
        INITIAL_STATE, "a<thinking>b</thinking>c<think>d</think>e"
    )
    assert segments == [("a", False), ("b", True), ("c", False), ("d", True), ("e", False)]
    assert state == INITIAL_STATE


def test_an_open_inside_a_block_is_text_and_the_first_close_ends_it():
    segments, state = split_inline_think_tags(INITIAL_STATE, "<think>a<think>b</think>c")
    assert segments == [("a<think>b", True), ("c", False)]
    assert state.in_thinking is False


@pytest.mark.parametrize("text", ["<b>bold</b>", "a < b and c > d", "x <", "5 <3"])
def test_a_literal_angle_bracket_is_visible_text(text):
    segments, state = split_inline_think_tags(INITIAL_STATE, text)
    if state.fragment:
        # A lone trailing "<" may be the start of a tag: it is held, not lost.
        rest, state = split_inline_think_tags(state, " done")
        segments += rest
    assert "".join(t for t, thinking in segments if not thinking) == text + (
        " done" if text.endswith("<") else ""
    )
    assert all(not thinking for _, thinking in segments)
    assert state.fragment == ""


def test_a_held_fragment_that_turns_out_not_to_be_a_tag_is_released_as_text():
    _, held = split_inline_think_tags(INITIAL_STATE, "see <th")
    assert held.fragment == "<th"
    segments, state = split_inline_think_tags(held, "ree things")
    assert segments == [("<three things", False)]
    assert state == INITIAL_STATE


def test_whitespace_after_a_complete_tag_is_kept():
    segments, _ = split_inline_think_tags(INITIAL_STATE, "<think>plan</think>\n\n answer")
    assert segments == [("plan", True), ("\n\n answer", False)]


def test_whitespace_inside_a_split_tag_is_dropped_but_not_when_the_tag_is_not_completed():
    _, held = split_inline_think_tags(INITIAL_STATE, "<think>plan</t")
    segments, _ = split_inline_think_tags(held, " not a tag")
    assert segments == [("</t not a tag", True)]


def test_a_whitespace_only_tail_is_dropped_and_the_state_survives():
    _, open_state = split_inline_think_tags(INITIAL_STATE, "<think>")
    segments, state = split_inline_think_tags(open_state, "  \n")
    assert segments == []
    assert state.in_thinking is True


def test_a_close_split_from_visible_mode_marks_what_led_into_it_as_reasoning():
    segments, state = split_inline_think_tags(INITIAL_STATE, "resumed</t")
    assert segments == [("resumed", True)]
    assert state == ThinkParseState(in_thinking=True, fragment="</t")
    segments, state = split_inline_think_tags(state, "hink>after")
    assert segments == [("after", False)]
    assert state == INITIAL_STATE


def test_a_block_left_open_stays_open_for_the_next_event():
    segments, state = split_inline_think_tags(INITIAL_STATE, "before<think>still thinking")
    assert segments == [("before", False), ("still thinking", True)]
    assert state == ThinkParseState(in_thinking=True, fragment="")


def test_nothing_in_nothing_out_returns_the_same_state_object():
    held = ThinkParseState(in_thinking=True, fragment="")
    assert split_inline_think_tags(held, "") == ([], held)
    assert split_inline_think_tags(held, "")[1] is held


def test_the_function_does_not_change_the_state_it_is_given():
    state = ThinkParseState(in_thinking=True, fragment="</t")
    split_inline_think_tags(state, "hink>x")
    assert state == ThinkParseState(in_thinking=True, fragment="</t")


def test_unicode_and_large_payloads_pass_through_whole():
    text = "思考 " * 50_000
    segments, state = split_inline_think_tags(INITIAL_STATE, f"<think>{text}</think>ответ")
    assert segments == [(text, True), ("ответ", False)]
    assert state == INITIAL_STATE
