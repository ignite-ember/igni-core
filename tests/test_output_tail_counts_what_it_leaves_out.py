"""``OutputTail``: the last lines, and exactly how much came before them."""

from __future__ import annotations

import pytest

from igni_core.core.tools.output_tail import OutputTail


def test_nothing_is_left_out_when_the_tail_is_larger_than_the_output():
    tail = OutputTail.of(["a", "b"], 10)

    assert tail.lines == ["a", "b"]
    assert (tail.omitted_lines, tail.omitted_chars, tail.total_lines, tail.cut) == (0, 0, 2, False)


def test_an_empty_output_is_not_a_cut():
    tail = OutputTail.of([], 5)

    assert (tail.lines, tail.total_lines, tail.cut) == ([], 0, False)
    assert tail.render("More.") == ""


@pytest.mark.parametrize("tail_size", [0, -3])
def test_a_tail_of_zero_or_less_shows_no_lines_and_leaves_out_all_of_them(tail_size):
    tail = OutputTail.of(["aa", "bbb"], tail_size)

    assert tail.lines == []
    assert (tail.omitted_lines, tail.omitted_chars, tail.total_lines) == (2, 3 + 4, 2)
    assert tail.cut is True


def test_every_left_out_line_counts_its_newline():
    tail = OutputTail.of(["a", "bb", "ccc", "d"], 1)

    assert tail.lines == ["d"]
    assert (tail.omitted_lines, tail.omitted_chars) == (3, 2 + 3 + 4)


def test_characters_are_counted_not_bytes():
    tail = OutputTail.of(["збірка", "ok"], 1)

    assert tail.omitted_chars == len("збірка") + 1 == 7


def test_one_very_long_line_counts_whole():
    tail = OutputTail.of(["x" * 100_000, "end"], 1)

    assert tail.omitted_chars == 100_001


def test_dropped_lines_alone_are_a_cut_even_when_nothing_is_hidden_here():
    tail = OutputTail.of(["a", "b"], 5, dropped_lines=4, dropped_chars=9)

    assert (tail.omitted_lines, tail.omitted_chars, tail.total_lines, tail.cut) == (4, 9, 6, True)


def test_a_cut_renders_a_note_the_marker_and_the_lines():
    tail = OutputTail.of(["aa", "bb", "cc", "dd"], 2)

    assert tail.render("Read the log.") == (
        "[Output truncated: 4 lines; the first 2 are not shown, only the last 2. "
        "Read the log.]\n"
        "... (6 characters truncated) ...\n\n"
        "cc\ndd"
    )


def test_a_cut_with_no_hint_still_closes_the_note():
    tail = OutputTail.of(["aa", "bb"], 1)

    assert tail.render("").startswith(
        "[Output truncated: 2 lines; the first 1 are not shown, only the last 1. ]\n"
    )


def test_an_uncut_output_renders_as_its_lines_joined_by_newlines():
    assert OutputTail.of(["a", "b", "c"], 3).render("ignored") == "a\nb\nc"
