"""``HookProvisioner``: scripts always, registration once, nothing when a group ships them.

Driven directly, on a real project directory.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

from igni_core.core.hooks.schemas import HookDefinition
from igni_core.core.init.hook_provisioner import HookProvisioner
from igni_core.core.init.hooks_catalog import BUILT_IN_HOOKS
from igni_core.core.init.schemas import BuiltInHookSpec
from igni_core.core.paths import CONFIG_DIR


def _settings(project: Path) -> dict:
    return json.loads((project / CONFIG_DIR / "settings.json").read_text())


def _commands(settings: dict, event: str) -> list[str]:
    return [h["command"] for h in settings["hooks"][event]]


def test_it_writes_every_catalog_script_executable_and_registers_each_under_its_event(tmp_path):
    HookProvisioner(project_dir=tmp_path).provision()

    for spec in BUILT_IN_HOOKS:
        script = tmp_path / CONFIG_DIR / "hooks" / spec.filename
        assert script.read_text() == spec.content
        assert script.stat().st_mode & stat.S_IXUSR
    settings = _settings(tmp_path)
    assert _commands(settings, "PreToolUse") == [f"{CONFIG_DIR}/hooks/pre-pr-review.sh"]
    assert _commands(settings, "PostToolUse") == [f"{CONFIG_DIR}/hooks/post-commit-todo.sh"]


def test_running_twice_registers_each_hook_once(tmp_path):
    HookProvisioner(project_dir=tmp_path).provision()
    HookProvisioner(project_dir=tmp_path).provision()

    settings = _settings(tmp_path)
    assert len(settings["hooks"]["PreToolUse"]) == 1
    assert len(settings["hooks"]["PostToolUse"]) == 1


def test_a_script_the_person_edited_is_put_back(tmp_path):
    HookProvisioner(project_dir=tmp_path).provision()
    script = tmp_path / CONFIG_DIR / "hooks" / "pre-pr-review.sh"
    script.write_text("#!/bin/sh\nexit 1\n")

    HookProvisioner(project_dir=tmp_path).provision()

    assert script.read_text() == BUILT_IN_HOOKS[0].content


def test_other_settings_keys_and_the_persons_own_hooks_survive(tmp_path):
    path = tmp_path / CONFIG_DIR / "settings.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "env": {"A": "1"},
                "hooks": {"PreToolUse": [{"type": "command", "command": "mine.sh"}]},
            }
        )
    )

    HookProvisioner(project_dir=tmp_path).provision()

    settings = _settings(tmp_path)
    assert settings["env"] == {"A": "1"}
    assert _commands(settings, "PreToolUse") == ["mine.sh", f"{CONFIG_DIR}/hooks/pre-pr-review.sh"]


def test_a_corrupt_settings_file_is_replaced_by_one_holding_the_hooks(tmp_path):
    path = tmp_path / CONFIG_DIR / "settings.json"
    path.parent.mkdir()
    path.write_text("{not json")

    HookProvisioner(project_dir=tmp_path).provision()

    assert _commands(_settings(tmp_path), "PreToolUse") == [f"{CONFIG_DIR}/hooks/pre-pr-review.sh"]


def test_without_registration_the_scripts_are_written_and_settings_is_untouched(tmp_path):
    HookProvisioner(project_dir=tmp_path, register_in_settings=False).provision()

    assert (tmp_path / CONFIG_DIR / "hooks" / "pre-pr-review.sh").exists()
    assert not (tmp_path / CONFIG_DIR / "settings.json").exists()


def test_without_registration_an_existing_settings_file_is_not_rewritten(tmp_path):
    path = tmp_path / CONFIG_DIR / "settings.json"
    path.parent.mkdir()
    path.write_text('{"env":{"A":"1"}}')

    HookProvisioner(project_dir=tmp_path, register_in_settings=False).provision()

    assert path.read_text() == '{"env":{"A":"1"}}'


def test_when_a_group_ships_the_scripts_nothing_at_all_is_written(tmp_path):
    HookProvisioner(project_dir=tmp_path, skip_entirely=True).provision()

    assert list(tmp_path.iterdir()) == []


def test_a_custom_catalog_replaces_the_built_in_one(tmp_path):
    spec = BuiltInHookSpec(
        filename="mine.sh",
        content="#!/bin/sh\n",
        event="Stop",
        definition=HookDefinition(type="command", command=f"{CONFIG_DIR}/hooks/mine.sh"),
    )

    HookProvisioner(project_dir=tmp_path, hooks=(spec,)).provision()

    hooks_dir = tmp_path / CONFIG_DIR / "hooks"
    assert [p.name for p in hooks_dir.iterdir()] == ["mine.sh"]
    assert list(_settings(tmp_path)["hooks"]) == ["Stop"]


def test_an_empty_catalog_writes_an_empty_settings_object(tmp_path):
    HookProvisioner(project_dir=tmp_path, hooks=()).provision()

    assert list((tmp_path / CONFIG_DIR / "hooks").iterdir()) == []
    assert _settings(tmp_path) == {}
