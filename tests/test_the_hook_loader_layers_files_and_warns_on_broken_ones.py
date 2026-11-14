"""The hook loader: which files it reads, in what order, and what a broken one costs.

Real files on disk through the real loader; only the home directory is
redirected into the test's folder.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from igni_core.core.hooks.loader import HookLoader, _SettingsPathDiscovery
from igni_core.core.hooks.registry import HookRegistry
from igni_core.core.paths import CONFIG_DIR


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake))
    return fake


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    folder.mkdir()
    return folder


def _hook(command: str) -> dict[str, str]:
    return {"type": "command", "command": command}


def _write_hooks(path: Path, command: str, event: str = "Stop") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": {event: [_hook(command)]}}), encoding="utf-8")


def _commands(loader: HookLoader, event: str = "Stop") -> list[str]:
    return [h.command for h in loader.load().registry.for_event(event)]


# ── which files, in what order ─────────────────────────────────────


def test_discover_lists_every_candidate_in_layer_order_without_filtering(home, project):
    loader = HookLoader(project, cross_tool_support=True, group_dir=project / "group")

    assert loader._discovery.discover() == [
        home / CONFIG_DIR / "settings.json",
        home / CONFIG_DIR / "settings.local.json",
        project / CONFIG_DIR / "settings.json",
        project / CONFIG_DIR / "settings.local.json",
        home / ".claude" / "settings.json",
        home / ".claude" / "settings.local.json",
        project / ".claude" / "settings.json",
        project / ".claude" / "settings.local.json",
        project / "group" / "settings.json",
    ]


def test_discover_without_cross_tool_support_or_group_lists_only_igni_files(home, project):
    loader = HookLoader(project)

    assert loader._discovery.discover() == [
        home / CONFIG_DIR / "settings.json",
        home / CONFIG_DIR / "settings.local.json",
        project / CONFIG_DIR / "settings.json",
        project / CONFIG_DIR / "settings.local.json",
    ]


def test_group_file_is_settings_json_in_the_group_dir_or_none(project):
    with_group = _SettingsPathDiscovery(project, cross_tool_support=False, group_dir=project / "g")
    without = _SettingsPathDiscovery(project, cross_tool_support=False)

    assert with_group.group_file == project / "g" / "settings.json"
    assert without.group_file is None


def test_the_same_event_in_several_files_runs_in_layer_order_with_the_group_last(home, project):
    group = project / "group"
    _write_hooks(group / "settings.json", "group")
    _write_hooks(project / ".claude" / "settings.local.json", "claude-project-local")
    _write_hooks(home / ".claude" / "settings.json", "claude-home")
    _write_hooks(project / CONFIG_DIR / "settings.local.json", "project-local")
    _write_hooks(project / CONFIG_DIR / "settings.json", "project")
    _write_hooks(home / CONFIG_DIR / "settings.local.json", "home-local")
    _write_hooks(home / CONFIG_DIR / "settings.json", "home")

    loader = HookLoader(project, cross_tool_support=True, group_dir=group)

    assert _commands(loader) == [
        "home",
        "home-local",
        "project",
        "project-local",
        "claude-home",
        "claude-project-local",
        "group",
    ]


def test_a_loader_given_no_project_dir_reads_the_working_directory(home, tmp_path, monkeypatch):
    folder = tmp_path / "cwd-project"
    _write_hooks(folder / CONFIG_DIR / "settings.json", "from-cwd")
    monkeypatch.setattr(Path, "cwd", lambda: folder)

    loader = HookLoader()

    assert loader.project_dir == folder
    assert _commands(loader) == ["from-cwd"]


# ── what a missing or broken file costs ────────────────────────────


def test_no_settings_files_give_an_empty_registry_and_no_warnings(home, project):
    result = HookLoader(project, cross_tool_support=True).load()

    assert result.registry.raw == {}
    assert result.warnings == []


def test_a_malformed_file_warns_and_the_others_still_load(home, project):
    broken = home / CONFIG_DIR / "settings.json"
    broken.parent.mkdir(parents=True)
    broken.write_text("{not json", encoding="utf-8")
    _write_hooks(project / CONFIG_DIR / "settings.json", "project")

    result = HookLoader(project).load()

    assert [h.command for h in result.registry.for_event("Stop")] == ["project"]
    (warning,) = result.warnings
    assert warning.kind == "invalid_json"
    assert warning.source == str(broken)


@pytest.mark.parametrize("document", ["[1, 2]", "42", '"text"', "null"])
def test_a_settings_file_that_is_not_an_object_warns_instead_of_raising(home, project, document):
    path = home / CONFIG_DIR / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(document, encoding="utf-8")
    _write_hooks(project / CONFIG_DIR / "settings.json", "project")

    result = HookLoader(project).load()

    assert [h.command for h in result.registry.for_event("Stop")] == ["project"]
    (warning,) = result.warnings
    assert (warning.kind, warning.source) == ("non_dict_block", str(path))


def test_a_settings_file_that_is_not_utf8_warns_instead_of_raising(home, project):
    path = home / CONFIG_DIR / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"hooks": {"Stop": "\xff\xfe"}}')
    _write_hooks(project / CONFIG_DIR / "settings.json", "project")

    result = HookLoader(project).load()

    assert [h.command for h in result.registry.for_event("Stop")] == ["project"]
    (warning,) = result.warnings
    assert (warning.kind, warning.source) == ("invalid_json", str(path))


def test_a_settings_file_that_cannot_be_read_warns_with_os_error(home, project):
    path = home / CONFIG_DIR / "settings.json"
    path.mkdir(parents=True)  # opening a directory fails with an OSError
    _write_hooks(project / CONFIG_DIR / "settings.json", "project")

    result = HookLoader(project).load()

    assert [h.command for h in result.registry.for_event("Stop")] == ["project"]
    (warning,) = result.warnings
    assert (warning.kind, warning.source) == ("os_error", str(path))


def test_a_hooks_key_that_is_not_an_object_warns_and_the_file_is_skipped(home, project):
    path = home / CONFIG_DIR / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hooks": ["x"], "other": 1}), encoding="utf-8")

    result = HookLoader(project).load()

    assert result.registry.raw == {}
    assert [w.kind for w in result.warnings] == ["non_dict_block"]


def test_a_settings_file_without_a_hooks_key_adds_nothing_and_says_nothing(home, project):
    path = home / CONFIG_DIR / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"model": "x"}), encoding="utf-8")

    result = HookLoader(project).load()

    assert result.registry.raw == {}
    assert result.warnings == []


# ── plugin hooks ───────────────────────────────────────────────────


def _plugin(tmp_path: Path, content: str | bytes | None) -> Path:
    root = tmp_path / "plugin"
    (root / "hooks").mkdir(parents=True)
    if isinstance(content, bytes):
        (root / "hooks" / "hooks.json").write_bytes(content)
    elif content is not None:
        (root / "hooks" / "hooks.json").write_text(content, encoding="utf-8")
    return root


def test_plugin_hooks_run_before_the_hooks_already_in_the_registry(home, project, tmp_path):
    _write_hooks(project / CONFIG_DIR / "settings.json", "project")
    loader = HookLoader(project)
    result = loader.load()
    root = _plugin(
        tmp_path,
        json.dumps({"Stop": [_hook("plugin-a"), _hook("plugin-b")], "Start": [_hook("s")]}),
    )

    merged = loader.load_plugin_hooks(root, result.registry)

    assert merged.registry is result.registry
    assert merged.warnings == []
    assert [h.command for h in result.registry.for_event("Stop")] == [
        "plugin-b",
        "plugin-a",
        "project",
    ]
    assert [h.command for h in result.registry.for_event("Start")] == ["s"]


def test_a_plugin_without_a_hooks_file_leaves_the_registry_alone(home, project, tmp_path):
    registry = HookRegistry.from_empty()

    for root in (_plugin(tmp_path, None), tmp_path / "no-such-plugin"):
        result = HookLoader(project).load_plugin_hooks(root, registry)

        assert result.registry is registry
        assert result.warnings == []
    assert registry.raw == {}


def test_a_plugin_hooks_path_that_is_a_directory_is_not_a_hooks_file(home, project, tmp_path):
    root = tmp_path / "plugin"
    (root / "hooks" / "hooks.json").mkdir(parents=True)
    registry = HookRegistry.from_empty()

    result = HookLoader(project).load_plugin_hooks(root, registry)

    assert result.warnings == []
    assert registry.raw == {}


@pytest.mark.parametrize(
    ("content", "kind"),
    [
        ("{oops", "invalid_json"),
        (b'{"Stop": "\xff"}', "invalid_json"),
        ("[1]", "non_dict_block"),
    ],
)
def test_a_broken_plugin_hooks_file_warns_and_adds_nothing(home, project, tmp_path, content, kind):
    root = _plugin(tmp_path, content)
    registry = HookRegistry.from_empty()

    result = HookLoader(project).load_plugin_hooks(root, registry)

    assert result.registry is registry
    assert registry.raw == {}
    (warning,) = result.warnings
    assert (warning.kind, warning.source) == (kind, str(root / "hooks" / "hooks.json"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_plugin_hooks_file_warns_with_os_error(home, project, tmp_path):
    root = _plugin(tmp_path, json.dumps({"Stop": [_hook("x")]}))
    hooks_file = root / "hooks" / "hooks.json"
    hooks_file.chmod(0)
    registry = HookRegistry.from_empty()
    try:
        result = HookLoader(project).load_plugin_hooks(root, registry)
    finally:
        hooks_file.chmod(0o600)

    assert registry.raw == {}
    (warning,) = result.warnings
    assert (warning.kind, warning.source) == ("os_error", str(hooks_file))
