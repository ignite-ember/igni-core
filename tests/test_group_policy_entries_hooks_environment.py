"""One pack entry, the hooks they merge into, the environment's pins and the run gate."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.environment import GroupPolicyEnvironment
from igni_core.core.config.group_policy.gate import GatePhase, RunGate
from igni_core.core.config.group_policy.hooks import HookDeclaration, HookSet


def _entry(content: str = "x", kind: str = "agents", name: str = "a", **extra) -> GroupPolicyEntry:
    return GroupPolicyEntry(
        kind=kind, entry_name=name, content=content, content_type="markdown", **extra
    )


class TestSourceFields:
    @pytest.mark.parametrize(
        "fields",
        [{"source_ref": "main"}, {"source_subdir": "p"}, {"source_ref": "m", "source_url": "  "}],
    )
    def test_a_ref_or_subdir_without_a_url_is_refused(self, fields: dict[str, str]):
        with pytest.raises(ValidationError, match="require source_url"):
            _entry(kind="plugins", **fields)

    def test_blank_strings_count_as_absent(self):
        entry = _entry(kind="plugins", source_url="", source_ref="", source_subdir="  ")
        assert entry.source_url == ""

    def test_a_ref_with_a_url_is_accepted(self):
        assert _entry(kind="plugins", source_url="u", source_ref="main").source_ref == "main"


class TestCheckName:
    @pytest.mark.parametrize(
        ("name", "reason"),
        [
            ("", "is empty"),
            ("   ", "is empty"),
            (".", "directory reference"),
            ("..", "directory reference"),
            ("a/b", "path separator"),
            ("a\\b", "path separator"),
            ("a\x00b", "NUL byte"),
            ("~user", "home reference"),
            ("C:x", "drive path"),
            ("a:", "drive path"),
        ],
    )
    def test_a_name_that_would_leave_the_cache_is_refused_with_its_reason(
        self, name: str, reason: str
    ):
        check = GroupPolicyEntry.check_name(name)
        assert not check.ok
        assert reason in check.reason

    def test_surrounding_whitespace_is_stripped_not_refused(self):
        check = GroupPolicyEntry.check_name("  reviewer\t")
        assert check.ok and check.name == "reviewer"

    @pytest.mark.parametrize("name", ["é-agent", "ab:c", "x"])
    def test_unicode_and_a_colon_past_the_second_character_are_plain_names(self, name: str):
        assert GroupPolicyEntry.check_name(name).ok

    def test_a_name_is_never_rewritten_to_make_it_safe(self):
        check = GroupPolicyEntry.check_name("../../tmp/x")
        assert not check.ok and check.name == ""


class TestAgentModel:
    CONTENT = "---\nname: reviewer\ndescription: d\n---\n\nBody text.\n"

    def _frontmatter(self, rendered: str) -> dict:
        return yaml.safe_load(rendered.split("---")[1])

    def test_without_a_model_the_content_is_returned_untouched(self):
        assert _entry(self.CONTENT).rendered_content() == self.CONTENT

    def test_a_model_is_written_into_the_frontmatter_and_the_body_kept(self):
        rendered = _entry(self.CONTENT, model="adapter-7").rendered_content()
        assert self._frontmatter(rendered) == {
            "name": "reviewer",
            "description": "d",
            "model": "adapter-7",
        }
        assert rendered.endswith("---\n\nBody text.\n")

    def test_an_existing_model_is_overwritten(self):
        content = "---\nname: r\nmodel: old\n---\nBody\n"
        rendered = _entry(content, model="new").rendered_content()
        assert self._frontmatter(rendered)["model"] == "new"
        assert "old" not in rendered

    def test_an_empty_frontmatter_block_gets_just_the_model(self):
        rendered = _entry("---\n\n---\nBody\n", model="m").rendered_content()
        assert rendered == "---\nmodel: m\n---\n\nBody\n"

    def test_content_without_frontmatter_is_returned_untouched_with_a_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            rendered = _entry("just a body", model="m").rendered_content()
        assert rendered == "just a body"
        assert "no YAML frontmatter" in caplog.text

    def test_unreadable_frontmatter_is_returned_untouched_with_a_warning(self, caplog):
        content = "---\nname: [unclosed\n---\nBody\n"
        with caplog.at_level(logging.WARNING):
            assert _entry(content, model="m").rendered_content() == content
        assert "unreadable frontmatter" in caplog.text

    def test_frontmatter_that_is_not_a_mapping_is_returned_untouched(self):
        content = "---\n- a\n- b\n---\nBody\n"
        assert _entry(content, model="m").rendered_content() == content


class TestMcpServerConfig:
    def test_json_content_is_parsed(self):
        assert _entry('{"command": "x"}', kind="mcps").mcp_server_config() == {"command": "x"}

    @pytest.mark.parametrize("content", ["not json", "", "   "])
    def test_anything_else_is_the_raw_text(self, content: str):
        assert _entry(content, kind="mcps").mcp_server_config() == content

    def test_a_json_scalar_is_parsed_as_one(self):
        assert _entry("5", kind="mcps").mcp_server_config() == 5


def _hook(content: object, name: str = "h") -> GroupPolicyEntry:
    text = content if isinstance(content, str) else json.dumps(content)
    return _entry(text, kind="hooks", name=name)


class TestHookSet:
    def test_a_bare_declaration_is_filed_under_its_event(self):
        hooks = HookSet()
        assert hooks.add(_hook({"event": "PreToolUse", "command": "a", "timeout": 5}))
        assert hooks.events["PreToolUse"][0].as_json() == {"command": "a", "timeout": 5}

    def test_a_single_declaration_in_a_block_is_wrapped_into_a_list(self):
        hooks = HookSet()
        hooks.add(_hook({"hooks": {"Stop": {"command": "a"}}}))
        assert [d.command for d in hooks.events["Stop"]] == ["a"]

    def test_entries_for_one_event_are_merged_in_order(self):
        hooks = HookSet()
        hooks.add(_hook({"hooks": {"Stop": [{"command": "a"}]}}))
        hooks.add(_hook({"event": "Stop", "command": "b"}))
        assert [d.command for d in hooks.events["Stop"]] == ["a", "b"]

    def test_invalid_json_is_refused_with_a_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert not HookSet().add(_hook("{nope", name="broken"))
        assert "'broken' is not valid JSON" in caplog.text

    @pytest.mark.parametrize("content", [[1], "5", {"command": "a"}, {"event": ""}, {"hooks": 3}])
    def test_content_that_names_no_event_is_refused_with_a_warning(self, content, caplog):
        hooks = HookSet()
        with caplog.at_level(logging.WARNING):
            assert not hooks.add(_hook(content, name="eventless"))
        assert hooks.events == {}
        assert "'eventless' names no event" in caplog.text

    def test_a_declaration_that_is_not_an_object_is_carried_as_sent(self, tmp_path: Path):
        hooks = HookSet()
        hooks.add(_hook({"hooks": {"Stop": ["bash x.sh", 3]}}))
        target = tmp_path / "settings.json"
        hooks.write_to(target)
        assert json.loads(target.read_text()) == {"hooks": {"Stop": ["bash x.sh", 3]}}

    def test_a_declaration_with_a_command_of_the_wrong_type_is_carried_as_sent(self, tmp_path):
        assert HookDeclaration.read({"command": 5}) == {"command": 5}
        assert HookDeclaration.read("x") == "x"

    def test_unknown_members_survive_into_the_file(self, tmp_path: Path):
        hooks = HookSet()
        hooks.add(_hook({"hooks": {"Stop": [{"command": "a", "matcher": "Bash"}]}}))
        target = tmp_path / "settings.json"
        hooks.write_to(target)
        assert json.loads(target.read_text())["hooks"]["Stop"] == [
            {"command": "a", "matcher": "Bash"}
        ]

    def test_the_file_is_removed_when_there_are_no_hooks_and_absence_is_tolerated(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_text("{}")
        HookSet().write_to(target)
        assert not target.exists()
        HookSet().write_to(target)  # nothing there: no error

    def test_the_placeholder_is_expanded_everywhere_it_appears_in_a_command(self, tmp_path):
        hooks = HookSet()
        hooks.add(
            _hook({"hooks": {"Stop": [{"command": "{group_scripts}/a && {group_scripts}/b"}]}})
        )
        hooks.add(_hook({"event": "Stop", "command": "{group_scripts}/c"}))
        expanded = hooks.expanded(tmp_path)
        assert [d.command for d in expanded.events["Stop"]] == [
            f"{tmp_path}/a && {tmp_path}/b",
            f"{tmp_path}/c",
        ]

    def test_a_placeholder_outside_the_command_and_a_missing_command_are_left_alone(self, tmp_path):
        hooks = HookSet()
        hooks.add(
            _hook({"hooks": {"Stop": [{"matcher": "{group_scripts}"}, {"command": "/abs/x"}]}})
        )
        expanded = hooks.expanded(tmp_path)
        assert expanded.events["Stop"][0].as_json() == {"matcher": "{group_scripts}"}
        assert expanded.events["Stop"][1].command == "/abs/x"

    def test_expanding_does_not_change_the_set_it_was_made_from(self, tmp_path):
        hooks = HookSet()
        hooks.add(_hook({"event": "Stop", "command": "{group_scripts}/a"}))
        hooks.expanded(tmp_path)
        assert hooks.events["Stop"][0].command == "{group_scripts}/a"


class TestEnvironment:
    def test_nothing_set_means_no_pins_and_not_required(self):
        env = GroupPolicyEnvironment.read({})
        assert env.pins == () and env.required is False

    @pytest.mark.parametrize("raw", ["a,b,c", "a b c", "a, b ,c", "a\nb\tc", " a,,b  ,c, "])
    def test_pins_are_split_on_commas_and_any_whitespace(self, raw: str):
        env = GroupPolicyEnvironment.read({GroupPolicyEnvironment.TRUSTED_KEYS_VAR: raw})
        assert env.pins == ("a", "b", "c")

    @pytest.mark.parametrize("raw", ["", "  ", ",,"])
    def test_an_empty_pin_list_is_no_pins(self, raw: str):
        assert (
            GroupPolicyEnvironment.read({GroupPolicyEnvironment.TRUSTED_KEYS_VAR: raw}).pins == ()
        )

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", " Yes ", "on", "ON"])
    def test_these_spellings_turn_required_on(self, raw: str):
        assert GroupPolicyEnvironment.read({GroupPolicyEnvironment.REQUIRED_VAR: raw}).required

    @pytest.mark.parametrize("raw", ["0", "false", "off", "no", "garbage", "", "2", "y"])
    def test_anything_else_leaves_it_off(self, raw: str):
        assert not GroupPolicyEnvironment.read({GroupPolicyEnvironment.REQUIRED_VAR: raw}).required

    def test_the_process_environment_is_the_default(self, monkeypatch):
        monkeypatch.setenv("IGNI_GROUP_POLICY_TRUSTED_KEYS", "k1,k2")
        monkeypatch.setenv("IGNI_GROUP_POLICY_REQUIRED", "1")
        env = GroupPolicyEnvironment.read()
        assert env.pins == ("k1", "k2") and env.required

    def test_an_explicit_mapping_is_used_instead_of_the_process_environment(self, monkeypatch):
        monkeypatch.setenv("IGNI_GROUP_POLICY_REQUIRED", "1")
        assert not GroupPolicyEnvironment.read({}).required

    def test_what_was_read_is_frozen(self):
        with pytest.raises(ValidationError):
            GroupPolicyEnvironment.read({}).required = True  # type: ignore[misc]


class TestRunGate:
    def test_a_new_gate_is_open_with_no_reason(self):
        gate = RunGate()
        assert gate.phase is GatePhase.open and gate.reason is None

    def test_closing_it_says_why(self):
        gate = RunGate()
        gate.close("policy required")
        assert gate.phase is GatePhase.closed and gate.reason == "policy required"

    def test_closing_again_keeps_it_closed_with_the_latest_reason(self):
        gate = RunGate()
        gate.close("first")
        gate.close("second")
        assert gate.phase is GatePhase.closed and gate.reason == "second"

    def test_two_gates_are_independent(self):
        closed, open_ = RunGate(), RunGate()
        closed.close("no")
        assert open_.phase is GatePhase.open

    def test_the_phases_are_the_strings_the_callers_compare_with(self):
        assert (GatePhase.open, GatePhase.closed) == ("open", "closed")
