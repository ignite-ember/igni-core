"""One agent ``.md`` file becomes one definition, or a specific refusal.

:class:`AgentMarkdownFile` reads the file once, caches what it parsed, and
says in its own words why a file is not an agent: unreadable, no frontmatter,
broken YAML, no name, no description.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.agents.markdown import AgentMarkdownFile
from igni_core.core.prompts.fragments import PromptFragments
from igni_core.core.prompts.loader import PromptLoader


def _file(path: Path, *, content: str | None = None, fragments: PromptFragments | None = None):
    loader = PromptLoader(fragments or PromptFragments())
    return AgentMarkdownFile(path, content=content, prompt_loader=loader)


def _write(tmp_path: Path, text: str | bytes, name: str = "a.md") -> Path:
    path = tmp_path / name
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


class TestWhyAFileIsNotAnAgent:
    def test_a_missing_file_cannot_be_read(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="Cannot read"):
            _file(tmp_path / "gone.md").parse()

    def test_a_file_that_is_not_utf8_cannot_be_read(self, tmp_path) -> None:
        path = _write(tmp_path, b"---\nname: \xff\xfe\n---\nbody")
        with pytest.raises(ValueError, match="Cannot read"):
            _file(path).parse()

    def test_no_frontmatter_is_said_so(self, tmp_path) -> None:
        path = _write(tmp_path, "just a body")
        with pytest.raises(ValueError, match="No YAML frontmatter found"):
            _file(path).parse()

    def test_frontmatter_never_closed_is_no_frontmatter(self, tmp_path) -> None:
        path = _write(tmp_path, "---\nname: a\ndescription: d\nbody")
        with pytest.raises(ValueError, match="No YAML frontmatter found"):
            _file(path).parse()

    def test_broken_yaml_is_called_invalid_not_missing(self, tmp_path) -> None:
        path = _write(tmp_path, "---\nname: [unclosed\n---\nbody")
        with pytest.raises(ValueError, match="Invalid YAML frontmatter"):
            _file(path).parse()

    @pytest.mark.parametrize("frontmatter", ["- a\n- b", "just a scalar"])
    def test_frontmatter_that_is_not_a_mapping_is_invalid(self, tmp_path, frontmatter) -> None:
        path = _write(tmp_path, f"---\n{frontmatter}\n---\nbody")
        with pytest.raises(ValueError, match="Invalid YAML frontmatter"):
            _file(path).parse()

    def test_empty_frontmatter_is_a_missing_name_not_a_crash(self, tmp_path) -> None:
        path = _write(tmp_path, "---\n\n---\nbody")
        with pytest.raises(ValueError, match="missing 'name'"):
            _file(path).parse()

    def test_a_description_is_required_after_the_name(self, tmp_path) -> None:
        path = _write(tmp_path, "---\nname: a\n---\nbody")
        with pytest.raises(ValueError, match="missing 'description'"):
            _file(path).parse()

    def test_a_field_of_the_wrong_type_is_a_value_error(self, tmp_path) -> None:
        path = _write(tmp_path, "---\nname: a\ndescription: d\nreasoning: maybe\n---\nbody")
        with pytest.raises(ValueError):
            _file(path).parse()


class TestWhatAFileSays:
    def test_tools_and_tags_accept_a_comma_string_with_stray_commas(self, tmp_path) -> None:
        path = _write(
            tmp_path, "---\nname: a\ndescription: d\ntools: ' Bash ,, Edit,'\ntags: x, y\n---\nb"
        )
        definition = _file(path).parse()
        assert definition.tools == ["Bash", "Edit"]
        assert definition.tags == ["x", "y"]

    def test_a_tools_list_is_stringified_item_by_item(self, tmp_path) -> None:
        path = _write(tmp_path, "---\nname: a\ndescription: d\ntools: [Bash, 7]\n---\nb")
        assert _file(path).parse().tools == ["Bash", "7"]

    @pytest.mark.parametrize("value", ["null", "5", "{a: b}"])
    def test_tools_of_any_other_shape_is_none(self, tmp_path, value) -> None:
        path = _write(tmp_path, f"---\nname: a\ndescription: d\ntools: {value}\n---\nb")
        assert _file(path).parse().tools == []

    def test_the_body_is_stripped_and_is_the_system_prompt(self, tmp_path) -> None:
        path = _write(tmp_path, "---\nname: a\ndescription: d\n---\n\n\n  You help.  \n\n")
        definition = _file(path).parse()
        assert definition.system_prompt == "You help."
        assert definition.source_path == path

    def test_an_empty_body_is_an_empty_prompt(self, tmp_path) -> None:
        path = _write(tmp_path, "---\nname: a\ndescription: d\n---")
        assert _file(path).parse().system_prompt == ""

    def test_crlf_line_endings_parse(self, tmp_path) -> None:
        path = _write(tmp_path, b"---\r\nname: a\r\ndescription: d\r\n---\r\nbody\r\n")
        definition = _file(path).parse()
        assert (definition.name, definition.description) == ("a", "d")
        assert definition.system_prompt == "body"

    def test_unicode_names_and_prompts_survive(self, tmp_path) -> None:
        path = _write(tmp_path, "---\nname: jäger\ndescription: 日本語\n---\nGrüße ✓")
        definition = _file(path).parse()
        assert (definition.name, definition.description) == ("jäger", "日本語")
        assert definition.system_prompt == "Grüße ✓"

    def test_a_temperature_of_zero_is_kept(self, tmp_path) -> None:
        path = _write(tmp_path, "---\nname: a\ndescription: d\ntemperature: 0\n---\nb")
        assert _file(path).parse().temperature == 0

    def test_empty_mcp_servers_is_an_empty_list(self, tmp_path) -> None:
        path = _write(tmp_path, "---\nname: a\ndescription: d\nmcp_servers:\n---\nb")
        assert _file(path).parse().mcp_servers == []

    def test_agent_fields_are_read_from_the_frontmatter(self, tmp_path) -> None:
        path = _write(
            tmp_path,
            "---\nname: a\ndescription: d\nmodel: m\ncolor: red\nreasoning: true\n"
            "reasoning_min_steps: 2\nreasoning_max_steps: 4\ncan_orchestrate: false\n"
            "max_turns: 9\ntemperature: 0.4\nmax_tokens: 700\n---\nb",
        )
        d = _file(path).parse()
        assert (d.model, d.color, d.reasoning) == ("m", "red", True)
        assert (d.reasoning_min_steps, d.reasoning_max_steps) == (2, 4)
        assert d.can_orchestrate is False
        assert (d.max_turns, d.temperature, d.max_tokens) == (9, 0.4, 700)

    def test_an_anchor_in_the_body_is_filled_and_an_unknown_one_stays(self, tmp_path) -> None:
        fragments = PromptFragments()
        fragments.register("SCHEMA", "acme.ext", lambda: "the schema")
        path = _write(tmp_path, "---\nname: a\ndescription: d\n---\nUse {{SCHEMA}} and {{OTHER}}.")
        assert _file(path, fragments=fragments).parse().system_prompt == (
            "Use the schema and {{OTHER}}."
        )


class TestReadingOnce:
    def test_supplied_content_is_parsed_without_touching_the_disk(self, tmp_path) -> None:
        path = tmp_path / "never-written.md"
        md = _file(path, content="---\nname: a\ndescription: d\n---\nbody")
        assert md.parse().system_prompt == "body"
        assert not path.exists()

    def test_the_file_is_read_once_for_keys_and_definition(self, tmp_path) -> None:
        path = _write(tmp_path, "---\nname: a\ndescription: d\nhooks: x\n---\nbody")
        md = _file(path)
        assert md.raw_frontmatter_keys() == {"name", "description", "hooks"}
        path.write_text("---\nname: changed\ndescription: d\n---\nother", encoding="utf-8")
        assert md.parse().name == "a"

    def test_raw_keys_are_empty_on_every_kind_of_failure(self, tmp_path) -> None:
        assert _file(tmp_path / "gone.md").raw_frontmatter_keys() == set()
        assert _file(_write(tmp_path, "no frontmatter", "n.md")).raw_frontmatter_keys() == set()
        assert (
            _file(_write(tmp_path, "---\nname: [x\n---\nb", "y.md")).raw_frontmatter_keys() == set()
        )
        assert _file(_write(tmp_path, "---\n- a\n---\nb", "l.md")).raw_frontmatter_keys() == set()
        assert _file(_write(tmp_path, b"---\n\xff\n---\n", "u.md")).raw_frontmatter_keys() == set()
