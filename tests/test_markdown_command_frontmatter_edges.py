"""Frontmatter edges a person's file can hit: Windows line endings, a fence
that never closes, values that are not text, and the log line a broken
header leaves."""

from __future__ import annotations

import logging

import pytest

from igni_core.core.utils import markdown_command_frontmatter as module
from igni_core.core.utils.markdown_command_frontmatter import CommandFrontmatter


def test_a_header_with_windows_line_endings_is_read():
    meta, body = CommandFrontmatter.parse(
        "---\r\ndescription: Hello\r\nmodel: opus\r\n---\r\nBody\r\n"
    )

    assert meta.description == "Hello"
    assert meta.model == "opus"
    assert body == "Body\r\n"


def test_a_fence_that_never_closes_leaves_the_whole_file_as_body():
    text = "---\ndescription: Hello\nBody.\n"

    meta, body = CommandFrontmatter.parse(text)

    assert meta == CommandFrontmatter()
    assert body == text


def test_a_file_that_merely_starts_with_dashes_in_a_line_is_not_a_header():
    text = "--- not a fence\ndescription: x\n---\nBody\n"

    meta, body = CommandFrontmatter.parse(text)

    assert meta == CommandFrontmatter()
    assert body == text


def test_a_header_value_holding_a_dash_run_inside_a_line_does_not_end_the_header():
    meta, body = CommandFrontmatter.parse("---\ndescription: a --- b\nmodel: opus\n---\nBody\n")

    assert meta.description == "a --- b"
    assert meta.model == "opus"
    assert body == "Body\n"


@pytest.mark.parametrize("value", ["0", "false", "''", "[]"])
def test_a_model_that_is_falsy_is_no_preference(value):
    meta, _ = CommandFrontmatter.parse(f"---\nmodel: {value}\n---\nBody\n")

    assert meta.model is None


def test_a_description_that_is_a_list_is_read_as_its_text():
    meta, _ = CommandFrontmatter.parse("---\ndescription: [a, b]\n---\nBody\n")

    assert meta.description == "['a', 'b']"


def test_a_description_that_is_a_boolean_is_read_as_its_text():
    meta, _ = CommandFrontmatter.parse("---\ndescription: true\n---\nBody\n")

    assert meta.description == "True"


def test_unicode_survives_in_the_header_and_the_body():
    meta, body = CommandFrontmatter.parse("---\ndescription: Привіт ✓\n---\nТіло\n")

    assert meta.description == "Привіт ✓"
    assert body == "Тіло\n"


def test_a_header_that_does_not_parse_is_logged_and_keeps_the_body(caplog):
    with caplog.at_level(logging.DEBUG, logger=module.logger.name):
        meta, body = CommandFrontmatter.parse("---\nkey: [unclosed\n---\nBody\n")

    assert meta == CommandFrontmatter()
    assert body == "Body\n"
    assert "frontmatter parse failed" in caplog.text


def test_allowed_tools_given_as_a_number_grants_nothing():
    meta, _ = CommandFrontmatter.parse("---\nallowed-tools: 5\n---\nBody\n")

    assert meta.allowed_tools == ()
