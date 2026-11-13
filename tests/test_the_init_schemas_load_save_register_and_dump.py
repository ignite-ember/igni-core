"""The init schemas: settings load/save, hook specs, home config, outcomes, defaults."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import igni_core
from igni_core.core.hooks.schemas import HookDefinition
from igni_core.core.init.schemas import (
    BuiltInHookSpec,
    HomeConfig,
    HomeModelRegistryEntry,
    InitConfig,
    InitResult,
    MigrationResult,
    SettingsFile,
    SyncOutcome,
    _default_package_dir,
)
from igni_core.core.init_templates import CONFIG_YAML_HEADER
from igni_core.core.paths import CONFIG_DIR


def _hook(command: str = "a.sh") -> HookDefinition:
    return HookDefinition(type="command", command=command)


# ── SettingsFile.load / load_if_valid ────────────────────────────


def test_load_of_an_absent_corrupt_or_wrong_shape_file_is_an_empty_instance(tmp_path):
    missing = tmp_path / "missing.json"
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"hooks": "nope"}))
    for path in (missing, corrupt, wrong):
        loaded = SettingsFile.load(path)
        assert loaded.hooks == {}
        assert loaded.permissions is None


def test_load_if_valid_refuses_only_a_present_file_that_fails_validation(tmp_path):
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"hooks": "nope"}))
    assert SettingsFile.load_if_valid(wrong) is None
    # absent and unparseable still give an empty, savable instance
    assert SettingsFile.load_if_valid(tmp_path / "missing.json") == SettingsFile()
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    assert SettingsFile.load_if_valid(corrupt) == SettingsFile()
    good = tmp_path / "good.json"
    good.write_text(
        json.dumps(
            {
                "hooks": {"Stop": [{"type": "command", "command": "x.sh"}]},
                "env": {"K": "V"},
            }
        )
    )
    loaded = SettingsFile.load_if_valid(good)
    assert loaded is not None
    assert loaded.hooks["Stop"][0].command == "x.sh"
    assert loaded.model_extra == {"env": {"K": "V"}}


# ── SettingsFile.save / register_hook ────────────────────────────


def test_an_empty_settings_file_saves_as_an_empty_object(tmp_path):
    path = tmp_path / "s.json"
    SettingsFile().save(path)
    assert json.loads(path.read_text()) == {}


def test_save_keeps_user_keys_and_omits_defaults(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"mcpServers": {"a": {"cmd": "x"}}, "env": {"K": "V"}}))
    settings = SettingsFile.load(path)
    settings.register_hook("Stop", _hook())
    settings.save(path)
    data = json.loads(path.read_text())
    assert data["mcpServers"] == {"a": {"cmd": "x"}}
    assert data["env"] == {"K": "V"}
    assert data["hooks"] == {"Stop": [{"type": "command", "command": "a.sh"}]}
    assert "permissions" not in data


def test_register_hook_dedups_by_command_within_one_event_only():
    settings = SettingsFile()
    settings.register_hook("Stop", _hook("a.sh"))
    settings.register_hook("Stop", HookDefinition(type="command", command="a.sh", timeout=5))
    assert len(settings.hooks["Stop"]) == 1
    assert settings.hooks["Stop"][0].timeout == 10000
    settings.register_hook("PreToolUse", _hook("a.sh"))
    settings.register_hook("Stop", _hook("b.sh"))
    assert [h.command for h in settings.hooks["Stop"]] == ["a.sh", "b.sh"]
    assert [h.command for h in settings.hooks["PreToolUse"]] == ["a.sh"]


# ── BuiltInHookSpec ──────────────────────────────────────────────


def _spec(content: str = "#!/bin/sh\n") -> BuiltInHookSpec:
    return BuiltInHookSpec(filename="h.sh", content=content, event="Stop", definition=_hook("h.sh"))


def test_a_hook_spec_is_frozen():
    with pytest.raises(ValidationError):
        _spec().filename = "other.sh"


def test_write_script_overwrites_and_adds_every_execute_bit(tmp_path):
    spec = _spec("echo é\n")
    target = tmp_path / "h.sh"
    target.write_text("old")
    target.chmod(0o600)
    spec.write_script(tmp_path)
    assert target.read_text(encoding="utf-8") == "echo é\n"
    mode = target.stat().st_mode
    assert mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH) == 0o111
    assert mode & stat.S_IRUSR and mode & stat.S_IWUSR


def test_write_script_into_a_missing_directory_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        _spec().write_script(tmp_path / "nope")


def test_register_in_registers_under_the_event_once():
    settings = SettingsFile()
    spec = _spec()
    spec.register_in(settings)
    spec.register_in(settings)
    assert [h.command for h in settings.hooks["Stop"]] == ["h.sh"]


# ── HomeModelRegistryEntry.is_bundled_cloud ──────────────────────


@pytest.mark.parametrize(
    ("url", "key", "bundled"),
    [
        ("https://api.ignite-ember.sh/v1", "cloud_token", True),
        ("https://api.ignite-ember.sh/v1", "sk-user", False),
        ("https://api.ignite-ember.sh/v1", "", False),
        ("https://my.host/v1", "cloud_token", False),
        ("", "cloud_token", False),
        ("https://api.ignite-ember.sh/v1", "cloud_token ", False),
    ],
)
def test_a_registry_entry_is_bundled_only_when_url_and_key_both_match(url, key, bundled):
    assert HomeModelRegistryEntry(url=url, api_key=key).is_bundled_cloud() is bundled


# ── HomeConfig.load / dump ───────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["a: [unclosed", "- 1\n- 2\n", "just a scalar", "models: 5\n", "models:\n  registry: [x]\n"],
)
def test_home_config_load_returns_none_for_unreadable_content(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    assert HomeConfig.load(path) is None


def test_home_config_load_of_a_missing_file_is_none_and_an_empty_file_is_empty(tmp_path):
    assert HomeConfig.load(tmp_path / "missing.yaml") is None
    path = tmp_path / "empty.yaml"
    path.write_text("")
    assert HomeConfig.load(path) == HomeConfig()


def test_home_config_round_trips_unknown_keys_after_the_header(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "theme: dark\nmodels:\n  default: m\n  extra_knob: 1\n  registry:\n"
        "    m:\n      url: u\n      api_key: k\n      temperature: 0.2\n"
    )
    config = HomeConfig.load(path)
    assert config is not None
    config.dump(path)
    text = path.read_text()
    assert text.startswith(CONFIG_YAML_HEADER)
    body = yaml.safe_load(text)
    assert body == {
        "theme": "dark",
        "models": {
            "default": "m",
            "extra_knob": 1,
            "registry": {"m": {"url": "u", "api_key": "k", "temperature": 0.2}},
        },
    }
    assert list(body) == ["models", "theme"]
    assert list(body["models"]) == ["default", "registry", "extra_knob"]


def test_dump_prunes_an_empty_default_registry_and_models_block(tmp_path):
    path = tmp_path / "config.yaml"
    HomeConfig.model_validate({"theme": "x", "models": {"default": "", "registry": {}}}).dump(path)
    assert yaml.safe_load(path.read_text()) == {"theme": "x"}
    HomeConfig(models=None).dump(path)
    assert path.read_text() == CONFIG_YAML_HEADER + "{}\n"
    HomeConfig.model_validate({"models": {"extra_knob": 1}}).dump(path)
    assert yaml.safe_load(path.read_text()) == {"models": {"extra_knob": 1}}


def test_dump_does_not_create_the_parent_directory(tmp_path):
    with pytest.raises(FileNotFoundError):
        HomeConfig().dump(tmp_path / "nope" / "config.yaml")


# ── Envelopes ────────────────────────────────────────────────────


def test_result_envelopes_default_empty():
    assert MigrationResult(ok=True).removed == []
    assert MigrationResult(ok=True).reason == ""
    result = InitResult(first_run=True)
    assert result.warnings == []
    assert result.migration is None
    assert InitResult(first_run=False).warnings is not result.warnings


def test_only_a_diverged_outcome_warns_and_it_names_the_sidecar():
    for kind in ("copied", "recorded_legacy", "unchanged", "overwritten"):
        assert SyncOutcome(kind=kind, key="agents/x.md").warning_message() is None
    message = SyncOutcome(kind="diverged", key="agents/x.md").warning_message()
    assert message == (
        "Built-in agents/x.md was updated but you have local modifications. "
        f"New version saved as {CONFIG_DIR}/agents/x.md.new — diff and merge at "
        "your convenience."
    )


def test_a_sync_outcome_kind_is_a_closed_set():
    with pytest.raises(ValidationError):
        SyncOutcome(kind="deleted", key="x")


# ── InitConfig ───────────────────────────────────────────────────


def test_init_config_defaults():
    config = InitConfig()
    assert config.marker_file == ".initialized"
    assert config.checksums_file == ".checksums.json"
    assert config.skip_builtin_hook_registration is False
    assert config.group_ships_hook_scripts is False
    assert config.skip_bundled_agents is False
    assert config.default_permissions.allow == ["WebSearch", "WebFetch"]
    assert config.default_permissions.ask == ["Write", "Edit", "Bash", "BashOutput"]
    assert config.default_permissions.deny == []


def test_default_permissions_are_not_shared_between_instances():
    first = InitConfig()
    first.default_permissions.allow.append("Bash")
    assert InitConfig().default_permissions.allow == ["WebSearch", "WebFetch"]


def test_the_default_package_dir_is_this_worktrees_package_root():
    root = Path(igni_core.__file__).resolve().parent
    assert _default_package_dir() == root
    assert InitConfig().package_dir == root
