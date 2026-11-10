"""The YAML header of a markdown-authored command.

A command file's header is written by a person, so the parser has to
be forgiving: a header that does not parse costs the command its
header, never the command itself. These tests pin that, and pin the
four fields igni reads out of it.

The header is a model, not a mapping — nothing downstream of this
module ever sees a raw dict.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.core.utils.markdown_command_frontmatter import CommandFrontmatter

# ── Splitting the header off ───────────────────────────────────


class TestParse:
    def test_a_file_with_no_header_is_all_body(self):
        meta, body = CommandFrontmatter.parse("Just a body, no header.\n")

        assert meta == CommandFrontmatter()
        assert body == "Just a body, no header.\n"

    def test_the_header_is_split_from_the_body(self):
        meta, body = CommandFrontmatter.parse(
            "---\ndescription: Quick status\nmodel: sonnet\n---\nBody.\n"
        )

        assert meta.description == "Quick status"
        assert meta.model == "sonnet"
        assert body == "Body.\n"

    def test_a_body_without_a_trailing_newline_survives(self):
        meta, body = CommandFrontmatter.parse("---\ndescription: d\n---\nBody.")

        assert body == "Body."

    def test_a_broken_header_costs_the_header_not_the_body(self):
        """YAML that does not parse is a typo in the header, not a
        broken command."""
        meta, body = CommandFrontmatter.parse("---\n: : not valid:\n  :\n---\nBody survives.\n")

        assert meta == CommandFrontmatter()
        assert "Body survives" in body

    def test_a_header_that_is_not_a_mapping_costs_the_header(self):
        """A scalar at the top level is not a header."""
        meta, body = CommandFrontmatter.parse("---\njust a string\n---\nBody.\n")

        assert meta == CommandFrontmatter()
        assert body == "Body.\n"

    def test_a_list_at_the_top_level_costs_the_header(self):
        meta, body = CommandFrontmatter.parse("---\n- one\n- two\n---\nBody.\n")

        assert meta == CommandFrontmatter()
        assert body == "Body.\n"

    def test_an_empty_header_yields_the_defaults(self):
        meta, body = CommandFrontmatter.parse("---\n\n---\nBody.\n")

        assert meta == CommandFrontmatter()
        assert body == "Body.\n"

    def test_only_the_first_block_is_the_header(self):
        """A second ``---`` fence further down the file is body."""
        meta, body = CommandFrontmatter.parse("---\ndescription: d\n---\nBody.\n---\nMore.\n")

        assert meta.description == "d"
        assert body == "Body.\n---\nMore.\n"


# ── The four fields ───────────────────────────────────────────


class TestTheTextFields:
    def test_a_description_is_read_as_prose(self):
        meta, _ = CommandFrontmatter.parse("---\ndescription: 2024\n---\nBody.\n")

        assert meta.description == "2024"

    def test_an_empty_description_is_empty(self):
        """``description:`` with nothing after it is an empty
        description, not the word "None"."""
        meta, _ = CommandFrontmatter.parse("---\ndescription:\n---\nBody.\n")

        assert meta.description == ""

    def test_a_description_is_stripped(self):
        meta, _ = CommandFrontmatter.parse("---\ndescription:   padded   \n---\nBody.\n")

        assert meta.description == "padded"

    def test_a_quoted_description_is_stripped(self):
        """YAML trims a plain scalar for us, but a quoted one keeps
        the spaces it was written with — the parser still trims."""
        meta, _ = CommandFrontmatter.parse('---\ndescription: "  padded  "\n---\nBody.\n')

        assert meta.description == "padded"

    def test_an_argument_hint_is_read_as_prose(self):
        meta, _ = CommandFrontmatter.parse("---\nargument-hint: 42\n---\nBody.\n")

        assert meta.argument_hint == "42"

    def test_an_empty_argument_hint_is_empty(self):
        meta, _ = CommandFrontmatter.parse("---\nargument-hint:\n---\nBody.\n")

        assert meta.argument_hint == ""


class TestTheModelField:
    def test_a_model_is_read_as_its_name(self):
        meta, _ = CommandFrontmatter.parse("---\nmodel: opus\n---\nBody.\n")

        assert meta.model == "opus"

    def test_an_empty_model_is_no_preference(self):
        """``model:`` with nothing after it means "no preference" —
        the session's ``/model`` decides — not an empty model name."""
        meta, _ = CommandFrontmatter.parse("---\nmodel:\n---\nBody.\n")

        assert meta.model is None

    def test_a_model_is_stripped(self):
        meta, _ = CommandFrontmatter.parse("---\nmodel:  opus  \n---\nBody.\n")

        assert meta.model == "opus"


class TestTheAllowedToolsField:
    def test_a_comma_separated_string_is_split(self):
        meta, _ = CommandFrontmatter.parse("---\nallowed-tools: Bash, Read\n---\nBody.\n")

        assert meta.allowed_tools == ("Bash", "Read")

    def test_a_yaml_list_is_read_as_a_list(self):
        meta, _ = CommandFrontmatter.parse("---\nallowed-tools:\n  - Bash\n  - Read\n---\nBody.\n")

        assert meta.allowed_tools == ("Bash", "Read")

    def test_the_names_are_stripped(self):
        meta, _ = CommandFrontmatter.parse("---\nallowed-tools:  Bash ,  Read \n---\nBody.\n")

        assert meta.allowed_tools == ("Bash", "Read")

    def test_an_empty_entry_is_dropped(self):
        """``Bash,,Read`` has two tools in it, not three."""
        meta, _ = CommandFrontmatter.parse("---\nallowed-tools: Bash,,Read\n---\nBody.\n")

        assert meta.allowed_tools == ("Bash", "Read")

    def test_a_list_entry_that_is_not_a_string_is_read_as_one(self):
        meta, _ = CommandFrontmatter.parse("---\nallowed-tools:\n  - 7\n  - Read\n---\nBody.\n")

        assert meta.allowed_tools == ("7", "Read")

    def test_an_empty_entry_in_a_list_is_dropped(self):
        meta, _ = CommandFrontmatter.parse("---\nallowed-tools:\n  - Bash\n  - ''\n---\nBody.\n")

        assert meta.allowed_tools == ("Bash",)

    def test_a_value_that_is_neither_text_nor_a_list_grants_nothing(self):
        """A number in the header is an authorship mistake; it must
        fail closed rather than grant a permission nobody asked for."""
        meta, _ = CommandFrontmatter.parse("---\nallowed-tools: 7\n---\nBody.\n")

        assert meta.allowed_tools == ()

    def test_a_mapping_grants_nothing(self):
        meta, _ = CommandFrontmatter.parse("---\nallowed-tools:\n  Bash: true\n---\nBody.\n")

        assert meta.allowed_tools == ()


# ── The mapping's keys are Claude Code's ───────────────────────


class TestFromMapping:
    def test_the_cc_keys_are_read(self):
        meta = CommandFrontmatter.from_mapping(
            {
                "description": "d",
                "allowed-tools": "Bash",
                "argument-hint": "[path]",
                "model": "opus",
            }
        )

        assert meta.description == "d"
        assert meta.allowed_tools == ("Bash",)
        assert meta.argument_hint == "[path]"
        assert meta.model == "opus"

    def test_an_unknown_key_is_ignored(self):
        """A command file may carry keys igni does not read."""
        meta = CommandFrontmatter.from_mapping({"description": "d", "future-key": "x"})

        assert meta.description == "d"

    def test_a_missing_key_is_the_default(self):
        meta = CommandFrontmatter.from_mapping({})

        assert meta == CommandFrontmatter()


# ── The model itself ──────────────────────────────────────────


class TestTheModel:
    def test_it_is_frozen(self):
        meta = CommandFrontmatter(description="d")

        with pytest.raises(ValidationError):
            meta.description = "other"  # type: ignore[misc]

    def test_it_compares_by_value(self):
        assert CommandFrontmatter(description="d") == CommandFrontmatter(description="d")

    def test_the_defaults_are_empty(self):
        meta = CommandFrontmatter()

        assert meta.description == ""
        assert meta.allowed_tools == ()
        assert meta.argument_hint == ""
        assert meta.model is None
