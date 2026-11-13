"""The skill parser: every frontmatter field, malformed files, SkillInfo."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from igni_core.core.skills.parser import SkillDefinition, SkillInfo, SkillParser


def _parse(tmp_path: Path, text: str, dirname: str = "my-skill") -> SkillDefinition:
    d = tmp_path / dirname
    d.mkdir()
    (d / "SKILL.md").write_text(text, encoding="utf-8")
    return SkillParser.parse(d / "SKILL.md")


def test_every_frontmatter_field_is_read(tmp_path):
    defn = _parse(
        tmp_path,
        "---\nname: deploy\ndescription: Ship it\nversion: '2.0'\ncategory: operations\n"
        "argument-hint: <env>\ncontext: fork\nagent: editor\nuser-invocable: false\n---\n\n"
        "  Body here\n\n",
    )
    assert defn.model_dump() == {
        "name": "deploy",
        "description": "Ship it",
        "version": "2.0",
        "category": "operations",
        "argument_hint": "<env>",
        "context": "fork",
        "agent": "editor",
        "user_invocable": False,
        "body": "Body here",
        "source_dir": tmp_path / "my-skill",
    }


def test_the_underscore_spelling_of_the_argument_hint_is_accepted(tmp_path):
    assert _parse(tmp_path, "---\nargument_hint: <x>\n---\nb").argument_hint == "<x>"


def test_an_argument_hint_given_as_a_yaml_list_is_joined_with_spaces(tmp_path):
    assert _parse(tmp_path, "---\nargument-hint: [<a>, <b>]\n---\nb").argument_hint == "<a> <b>"


def test_an_empty_argument_hint_is_an_empty_string(tmp_path):
    assert _parse(tmp_path, "---\nargument-hint:\n---\nb").argument_hint == ""


def test_an_empty_frontmatter_block_yields_the_defaults(tmp_path):
    defn = _parse(tmp_path, "---\n\n---\nBody\n")
    assert (defn.name, defn.body, defn.context, defn.agent) == ("my-skill", "Body", "inline", None)


def test_frontmatter_without_a_closing_delimiter_is_all_body(tmp_path):
    text = "---\nname: x\nstill going\n"
    defn = _parse(tmp_path, text)
    assert defn.name == "my-skill"
    assert defn.body == text.strip()


def test_a_body_may_contain_horizontal_rules(tmp_path):
    defn = _parse(tmp_path, "---\nname: x\n---\none\n---\ntwo\n")
    assert defn.body == "one\n---\ntwo"


def test_crlf_files_parse_like_lf_files(tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    (d / "SKILL.md").write_bytes(b"---\r\nname: win\r\ndescription: d\r\n---\r\nBody\r\n")
    defn = SkillParser.parse(d / "SKILL.md")
    assert (defn.name, defn.description, defn.body) == ("win", "d", "Body")


def test_invalid_yaml_raises_for_the_loader_to_report(tmp_path):
    with pytest.raises(yaml.YAMLError):
        _parse(tmp_path, "---\nname: [unclosed\n---\nbody\n")


def test_a_frontmatter_that_is_not_a_mapping_is_refused_with_a_clear_error(tmp_path):
    with pytest.raises(ValueError, match="mapping"):
        _parse(tmp_path, "---\n- a\n- b\n---\nbody\n")


def test_a_skill_with_a_non_ascii_body_reads_on_a_non_utf8_locale(tmp_path):
    d = tmp_path / "u"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: u\n---\nnaïve — ✓", encoding="utf-8")
    code = (
        "import sys\nfrom pathlib import Path\n"
        "from igni_core.core.skills.parser import SkillParser\n"
        "sys.stdout.buffer.write(SkillParser.parse(Path(sys.argv[1])).body.encode('utf-8'))\n"
    )
    env = {
        **os.environ,
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
        "PYTHONIOENCODING": "utf-8",
    }
    done = subprocess.run(
        [sys.executable, "-c", code, str(d / "SKILL.md")],
        env=env,
        capture_output=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr.decode()
    assert done.stdout.decode("utf-8") == "naïve — ✓"


def test_the_skill_dir_tokens_stay_literal_without_a_source_dir():
    body = "${IGNI_SKILL_DIR}/${CLAUDE_SKILL_DIR}/${IGNI_SESSION_ID}"
    assert SkillDefinition(name="x", body=body).render() == "${IGNI_SKILL_DIR}/${CLAUDE_SKILL_DIR}/"


def test_the_session_and_dir_tokens_both_render(tmp_path):
    defn = SkillDefinition(
        name="x", body="${IGNI_SKILL_DIR}|${IGNI_SESSION_ID}", source_dir=tmp_path
    )
    assert defn.render(session_id="s9") == f"{tmp_path}|s9"


def test_a_skill_info_defaults_and_published_descriptions():
    info = SkillInfo(name="deploy")
    assert info.model_dump() == {
        "name": "deploy",
        "description": "",
        "version": "0.1.0",
        "category": "development",
        "argument_hint": "",
        "context": "inline",
        "agent": "",
        "user_invocable": True,
        "body": "",
        "source_dir": "",
    }
    props = SkillInfo.model_json_schema()["properties"]
    assert all(p.get("description") for p in props.values())
