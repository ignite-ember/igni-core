"""``TrainedLayout.instructions`` rewrites only a list of two or more with
markdown on; everything else is handed back as it came."""

from __future__ import annotations

from igni_core.core.agents.trained_layout import MARKDOWN_LINE, TrainedLayout


def test_the_markdown_line_is_verbatim():
    assert MARKDOWN_LINE == "Use markdown to format your answers."


def test_none_passes_through_with_the_markdown_flag_as_given():
    assert TrainedLayout.instructions(None, markdown=True) == (None, True)
    assert TrainedLayout.instructions(None, markdown=False) == (None, False)


def test_an_empty_list_is_the_same_list():
    empty: list[str] = []

    result, markdown = TrainedLayout.instructions(empty, markdown=True)

    assert result is empty and markdown is True


def test_one_instruction_is_left_to_agno():
    one = ["only\nthis"]

    result, markdown = TrainedLayout.instructions(one, markdown=True)

    assert result is one and markdown is True


def test_without_markdown_a_list_is_left_to_agno():
    several = ["a", "b"]

    result, markdown = TrainedLayout.instructions(several, markdown=False)

    assert result is several and markdown is False


def test_several_become_one_bulleted_block_with_the_markdown_section_and_markdown_off():
    result, markdown = TrainedLayout.instructions(["a", "b\nc"], markdown=True)

    assert markdown is False
    assert result == [
        "- a\n- b\nc\n<additional_information>\n- Use markdown to format your answers.\n"
        "</additional_information>"
    ]


def test_an_item_that_is_not_a_string_is_formatted_as_one():
    result, _ = TrainedLayout.instructions(["a", 7, None], markdown=True)

    assert result is not None
    assert result[0].startswith("- a\n- 7\n- None\n<additional_information>")


def test_unicode_is_kept_whole():
    result, _ = TrainedLayout.instructions(["héllo ✓", "日本"], markdown=True)

    assert result is not None
    assert result[0].startswith("- héllo ✓\n- 日本\n")


def test_the_input_list_is_not_modified():
    original = ["a", "b"]

    TrainedLayout.instructions(original, markdown=True)

    assert original == ["a", "b"]
