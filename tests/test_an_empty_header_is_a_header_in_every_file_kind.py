"""A file that opens ``---`` then ``---`` with nothing between has an empty header.

The four frontmatter parsers needed a line between the fences, so that header was
not seen: the two fence lines reached the model as the start of the body, while
the same header with a blank line inside was stripped."""

from __future__ import annotations

import pytest

from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.output_styles.loader import _parse_frontmatter as parse_style
from igni_core.core.utils.context_frontmatter import parse_frontmatter as parse_rule
from igni_core.core.utils.markdown_command_frontmatter import CommandFrontmatter

EMPTY = "---\n---\nBody line.\n"
BLANK = "---\n\n---\nBody line.\n"


@pytest.mark.parametrize("content", [EMPTY, BLANK, "---  \n---  \nBody line.\n"])
def test_a_rules_file_with_an_empty_header_gives_its_body(content):
    assert parse_rule(content) == ([], "Body line.\n")


@pytest.mark.parametrize("content", [EMPTY, BLANK])
def test_a_command_with_an_empty_header_gives_its_body_and_no_settings(content):
    header, body = CommandFrontmatter.parse(content)

    assert body == "Body line.\n"
    assert header == CommandFrontmatter()


@pytest.mark.parametrize("content", [EMPTY, BLANK])
def test_an_output_style_with_an_empty_header_gives_its_body(content):
    assert parse_style(content) == ({}, "Body line.\n")


def test_a_group_agent_with_an_empty_header_gets_its_model_written_into_it():
    entry = GroupPolicyEntry(
        kind="agents", entry_name="a", content=EMPTY, content_type="markdown", model="m-1"
    )

    rendered = entry.rendered_content()

    assert rendered.startswith("---\n")
    assert "model: m-1" in rendered
    assert rendered.endswith("Body line.\n")
    assert rendered.count("---") == 2


def test_a_header_with_keys_is_read_as_before():
    assert parse_rule("---\npaths: [docs/**]\n---\nB") == (["docs/**"], "B")
    assert parse_rule("no header\n---\n") == ([], "no header\n---\n")
