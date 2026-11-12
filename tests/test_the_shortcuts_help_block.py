"""``/help shortcuts``: the keyboard shortcuts, as markdown, in order."""

from __future__ import annotations

from igni_core.backend.cmd_help import cmd_help
from igni_core.backend.keyboard_shortcuts_help import KeyboardShortcutsHelp, ShortcutItem

EXPECTED = (
    "## Keyboard Shortcuts\n"
    "- `Enter` — send message\n"
    "- `\\ + Enter` — new line\n"
    "- `Ctrl+D` — quit\n"
    "- `Ctrl+L` — clear screen\n"
    "- `Ctrl+O` — expand/collapse all messages\n"
    "- `Ctrl+V` — toggle verbose mode\n"
    "- `Up/Down` — input history\n"
    "- `Escape` — cancel\n"
)


def test_the_block_lists_the_eight_shortcuts_in_order_under_its_header() -> None:
    assert KeyboardShortcutsHelp.markdown() == EXPECTED


def test_the_block_is_the_same_every_time() -> None:
    assert KeyboardShortcutsHelp.markdown() == KeyboardShortcutsHelp.markdown()


def test_a_row_is_the_chord_in_code_an_em_dash_and_the_description() -> None:
    line = ShortcutItem(keys="Ctrl+K", description="kill line").to_markdown_line()

    assert line == "- `Ctrl+K` \N{EM DASH} kill line"


async def test_the_help_topic_shows_that_block() -> None:
    result = await cmd_help(None, "shortcuts")  # type: ignore[arg-type]

    assert result.content == EXPECTED
