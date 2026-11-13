"""Where a pack is cached, what each kind writes there, and what a rebuild restores.

The writer and every reader have to land on the same directory, so the layout
is derived in one place. Each kind then decides its file name, its mode and what
counts as stale; the cache composes them and keeps nothing on disk that looks
like a policy that is not one.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from igni_core.core.config.group_policy.cache import GroupPolicyCache
from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.kinds import KindRegistry
from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.group_policy.plugins import PluginInstallCoordinator

PLUGIN_URL = "https://example.invalid/tool.git"


def _entry(kind: str, name: str, content: str = "body", **extra) -> GroupPolicyEntry:
    return GroupPolicyEntry(
        kind=kind, entry_name=name, content=content, content_type="markdown", **extra
    )


def _pack(*entries: GroupPolicyEntry) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g-1",
        group_name="Legal",
        fetched_at=datetime.now(timezone.utc),
        entries=list(entries),
    )


@pytest.fixture
def cache(tmp_path: Path) -> GroupPolicyCache:
    return GroupPolicyCache(GroupPolicyLayout(root=tmp_path / "group-policy"))


class FakeInstaller:
    """Stands in for the git-backed installer: records what it was asked to install."""

    def __init__(self) -> None:
        self.installed: list[str] = []
        self._directory: Path | None = None

    def release_installed_from(self, url: str) -> list[Path]:
        return []

    def scoped_to(self, directory: Path) -> FakeInstaller:
        self._directory = directory
        return self

    def install(self, url: str, *, ref, subdir, directory_name: str) -> None:
        assert self._directory is not None
        (self._directory / directory_name).mkdir(parents=True)
        self.installed.append(directory_name)


def _home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Make ``home`` the home directory, however this test run has configured it."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


class TestLayoutDerivation:
    def test_it_is_group_policy_under_the_data_dir_for_text_and_paths(self, tmp_path: Path):
        assert GroupPolicyLayout.for_data_dir(str(tmp_path)).root == tmp_path / "group-policy"
        assert GroupPolicyLayout.for_data_dir(tmp_path).root == tmp_path / "group-policy"

    def test_a_tilde_is_expanded(self, tmp_path: Path, monkeypatch):
        _home(monkeypatch, tmp_path)
        assert GroupPolicyLayout.for_data_dir("~/data").root == tmp_path / "data" / "group-policy"

    @pytest.mark.parametrize("data_dir", [None, "", "   ", MagicMock(), 5, SimpleNamespace()])
    def test_a_missing_or_nonsensical_data_dir_falls_back_to_the_home_config_dir(
        self, data_dir: object, tmp_path: Path, monkeypatch
    ):
        _home(monkeypatch, tmp_path)
        layout = GroupPolicyLayout.for_data_dir(data_dir)  # type: ignore[arg-type]
        assert layout.root == tmp_path / ".igni" / "group-policy"

    def test_an_install_still_on_the_old_home_directory_is_not_told_it_has_no_policy(
        self, tmp_path: Path, monkeypatch
    ):
        _home(monkeypatch, tmp_path)
        (tmp_path / ".ember").mkdir()
        assert GroupPolicyLayout.for_data_dir(None).root == tmp_path / ".ember" / "group-policy"

    def test_the_new_home_directory_wins_when_both_exist(self, tmp_path: Path, monkeypatch):
        _home(monkeypatch, tmp_path)
        (tmp_path / ".ember").mkdir()
        (tmp_path / ".igni").mkdir()
        assert GroupPolicyLayout.for_data_dir(None).root == tmp_path / ".igni" / "group-policy"

    def test_the_cache_for_a_data_dir_uses_the_same_derivation(self, tmp_path: Path):
        cache = GroupPolicyCache.for_data_dir(tmp_path)
        assert cache.layout == GroupPolicyLayout.for_data_dir(tmp_path)


class TestLayoutFiles:
    def test_each_file_has_its_fixed_name_under_the_root(self, tmp_path: Path):
        layout = GroupPolicyLayout(root=tmp_path)
        assert layout.file(GroupPolicyLayout.SIGNED_PACK_FILE) == tmp_path / "pack.jws"
        assert layout.file(GroupPolicyLayout.JWKS_FILE) == tmp_path / "jwks.json"
        assert layout.file(GroupPolicyLayout.STATE_FILE) == tmp_path / "pack_state.json"
        assert layout.file(GroupPolicyLayout.META_FILE) == tmp_path / "pack_meta.json"
        assert (
            layout.file(GroupPolicyLayout.LEGACY_SETTINGS_FILE) == tmp_path / "group_settings.json"
        )
        assert layout.hooks_file == tmp_path / "hooks" / "settings.json"

    def test_a_kind_directory_is_not_created_until_asked_for(self, tmp_path: Path):
        layout = GroupPolicyLayout(root=tmp_path / "gp")
        assert layout.dir_for("agents") == tmp_path / "gp" / "agents"
        assert not layout.dir_for("agents").exists()
        assert layout.ensure_dir("agents").is_dir()

    def test_the_cache_creates_the_agents_and_scripts_directories_on_access(self, cache):
        assert cache.agents_dir == cache.layout.root / "agents" and cache.agents_dir.is_dir()
        assert cache.scripts_dir == cache.layout.root / "scripts" and cache.scripts_dir.is_dir()

    def test_a_kind_ships_only_when_its_directory_is_a_directory_with_something_in_it(
        self, tmp_path: Path
    ):
        layout = GroupPolicyLayout(root=tmp_path)
        assert not layout.ships("agents") and layout.shipped_dir("agents") is None
        layout.ensure_dir("agents")
        assert not layout.ships("agents") and layout.shipped_dir("agents") is None
        (layout.dir_for("agents") / "a.md").write_text("x")
        assert layout.ships("agents")
        assert layout.shipped_dir("agents") == tmp_path / "agents"

    def test_a_kind_that_is_a_plain_file_does_not_ship(self, tmp_path: Path):
        (tmp_path / "agents").write_text("not a directory")
        assert not GroupPolicyLayout(root=tmp_path).ships("agents")

    def test_a_directory_that_cannot_be_listed_does_not_ship(self, tmp_path: Path):
        layout = GroupPolicyLayout(root=tmp_path)
        directory = layout.ensure_dir("agents")
        (directory / "a.md").write_text("x")
        directory.chmod(0)
        try:
            if _listable(directory):
                pytest.skip("this account can list a mode 0 directory")
            assert not layout.ships("agents")
        finally:
            directory.chmod(0o700)

    def test_a_plugin_lives_under_plugins_by_its_stripped_name_and_an_unsafe_one_nowhere(
        self, tmp_path: Path
    ):
        layout = GroupPolicyLayout(root=tmp_path)
        assert layout.plugin_dir(" tool ") == tmp_path / "plugins" / "tool"
        assert layout.plugin_dir("../escape") is None
        assert layout.plugin_dir("") is None


def _listable(directory: Path) -> bool:
    try:
        list(directory.iterdir())
    except OSError:
        return False
    return True


class TestPlacement:
    def test_the_name_is_stripped_into_the_template(self, tmp_path: Path):
        placement = GroupPolicyLayout(root=tmp_path).place(
            "agents", "{name}.md", _entry("agents", "  reviewer ")
        )
        assert placement.ok and placement.path == tmp_path / "agents" / "reviewer.md"

    def test_an_unsafe_name_is_refused_with_the_name_checks_reason(self, tmp_path: Path):
        placement = GroupPolicyLayout(root=tmp_path).place(
            "agents", "{name}.md", _entry("agents", "a/b")
        )
        assert not placement.ok and placement.path is None
        assert "path separator" in placement.reason

    def test_an_entry_file_that_is_a_symlink_out_of_the_cache_is_refused(self, tmp_path: Path):
        outside = tmp_path / "outside.md"
        outside.write_text("keep")
        root = tmp_path / "gp"
        (root / "agents").mkdir(parents=True)
        (root / "agents" / "reviewer.md").symlink_to(outside)
        cache = GroupPolicyCache(GroupPolicyLayout(root=root))
        cache.materialize(_pack(_entry("agents", "reviewer", "overwritten")))
        assert outside.read_text() == "keep"


class TestLegacySettingsFile:
    def _seed(self, cache: GroupPolicyCache) -> Path:
        cache.layout.root.mkdir(parents=True, exist_ok=True)
        legacy = cache.layout.file(GroupPolicyLayout.LEGACY_SETTINGS_FILE)
        legacy.write_text(json.dumps({"permissions": {"mode": "bypass"}}))
        return legacy

    def test_materialising_removes_the_unsigned_copy(self, cache: GroupPolicyCache):
        legacy = self._seed(cache)
        cache.materialize(_pack(_entry("agents", "a")))
        assert not legacy.exists()

    def test_clearing_what_is_derived_removes_it_too(self, cache: GroupPolicyCache):
        legacy = self._seed(cache)
        cache.clear_derived()
        assert not legacy.exists()


class TestClear:
    def test_clear_removes_the_whole_directory(self, cache: GroupPolicyCache):
        cache.materialize(_pack(_entry("agents", "a")))
        cache.clear()
        assert not cache.layout.root.exists()

    def test_clear_with_no_directory_is_not_an_error(self, cache: GroupPolicyCache):
        assert not cache.layout.root.exists()
        cache.clear()

    def test_clear_derived_keeps_the_signed_pack_and_the_keys(self, cache: GroupPolicyCache):
        cache.materialize(_pack(_entry("agents", "a")))
        for name in (GroupPolicyLayout.SIGNED_PACK_FILE, GroupPolicyLayout.JWKS_FILE):
            cache.layout.file(name).write_text("kept")
        cache.clear_derived()
        assert not cache.layout.dir_for("agents").exists()
        assert cache.layout.file(GroupPolicyLayout.SIGNED_PACK_FILE).read_text() == "kept"
        assert cache.layout.file(GroupPolicyLayout.JWKS_FILE).read_text() == "kept"


class TestRebuild:
    def test_what_was_deleted_is_written_back_edited_is_overwritten_added_is_pruned(
        self, cache: GroupPolicyCache
    ):
        pack = _pack(_entry("agents", "kept"), _entry("agents", "gone"), _entry("rules", "r"))
        cache.materialize(pack)
        agents = cache.layout.dir_for("agents")
        (agents / "gone.md").unlink()
        (agents / "kept.md").write_text("edited by someone")
        (agents / "planted.md").write_text("added beside")

        result = cache.rebuild(pack)

        assert result.ok and result.reason == "" and result.missing_plugins == []
        assert (agents / "gone.md").read_text() == "body"
        assert (agents / "kept.md").read_text() == "body"
        assert not (agents / "planted.md").exists()

    def test_a_write_that_fails_is_reported_not_raised(self, cache: GroupPolicyCache):
        cache.layout.root.mkdir(parents=True)
        (cache.layout.root / "agents").write_text("a file where a directory goes")
        result = cache.rebuild(_pack(_entry("agents", "a")))
        assert not result.ok and result.reason

    def test_a_plugin_that_is_not_installed_is_reported_with_the_remedy_and_not_cloned(
        self, tmp_path: Path
    ):
        installer = FakeInstaller()
        layout = GroupPolicyLayout(root=tmp_path / "gp")
        cache = GroupPolicyCache(layout, installer)  # type: ignore[arg-type]
        result = cache.rebuild(_pack(_entry("plugins", "tool", source_url=PLUGIN_URL)))
        assert not result.ok
        assert result.missing_plugins == ["tool"]
        assert "plugin(s) tool are not installed" in result.reason
        assert "reinstalls them" in result.reason
        assert installer.installed == []

    def test_an_installed_plugin_makes_the_rebuild_whole(self, tmp_path: Path):
        layout = GroupPolicyLayout(root=tmp_path / "gp")
        (layout.dir_for("plugins") / "tool").mkdir(parents=True)
        cache = GroupPolicyCache(layout)
        assert cache.rebuild(_pack(_entry("plugins", "tool", source_url=PLUGIN_URL))).ok

    def test_a_normal_materialise_does_install_the_plugin(self, tmp_path: Path):
        installer = FakeInstaller()
        cache = GroupPolicyCache(GroupPolicyLayout(root=tmp_path / "gp"), installer)  # type: ignore[arg-type]
        cache.materialize(_pack(_entry("plugins", "tool", source_url=PLUGIN_URL)))
        assert installer.installed == ["tool"]

    def test_the_plugin_coordinator_is_the_one_the_facade_exposes(self, cache: GroupPolicyCache):
        assert isinstance(cache.plugins, PluginInstallCoordinator)


class TestKinds:
    def test_a_script_is_written_mode_0700_not_0755(self, cache: GroupPolicyCache):
        cache.materialize(_pack(_entry("scripts", "s", "#!/bin/bash\n")))
        assert (cache.scripts_dir / "s.sh").stat().st_mode & 0o777 == 0o700

    def test_only_scripts_are_executable(self, cache: GroupPolicyCache):
        cache.materialize(_pack(_entry("tools", "t", "x = 1\n"), _entry("agents", "a")))
        assert (cache.layout.dir_for("tools") / "t.py").stat().st_mode & 0o111 == 0
        assert (cache.agents_dir / "a.md").stat().st_mode & 0o111 == 0

    def test_an_agents_model_reaches_its_file_and_a_command_ignores_the_field(self, cache):
        agent = "---\nname: a\ndescription: d\n---\nbody\n"
        cache.materialize(
            _pack(
                _entry("agents", "a", agent, model="adapter"),
                _entry("commands", "c", agent, model="adapter"),
            )
        )
        assert "model: adapter" in (cache.agents_dir / "a.md").read_text()
        assert (cache.layout.dir_for("commands") / "c.md").read_text() == agent

    def test_mcp_content_that_is_not_json_is_kept_as_text_in_the_envelope(self, cache):
        cache.materialize(_pack(_entry("mcps", "srv", "not json")))
        written = json.loads((cache.layout.dir_for("mcps") / "srv.json").read_text())
        assert written == {"mcpServers": {"srv": "not json"}}

    def test_an_extension_entry_is_ignored_with_a_warning_and_writes_nothing(
        self, cache: GroupPolicyCache, caplog
    ):
        with caplog.at_level(logging.WARNING):
            cache.materialize(_pack(_entry("extensions", "ext", "{}")))
        assert "'ext' is an extension" in caplog.text
        assert "Ignored." in caplog.text
        assert not cache.layout.dir_for("extensions").exists()

    def test_an_extensions_directory_is_not_one_the_pack_owns(self, cache: GroupPolicyCache):
        directory = cache.layout.ensure_dir("extensions")
        (directory / "installed-elsewhere").write_text("keep")
        cache.materialize(_pack(_entry("agents", "a")))
        cache.clear_derived()
        assert (directory / "installed-elsewhere").read_text() == "keep"

    def test_a_stray_file_of_another_suffix_is_not_stale(self, cache: GroupPolicyCache):
        cache.materialize(_pack(_entry("agents", "a")))
        (cache.agents_dir / "notes.txt").write_text("not ours to remove")
        cache.materialize(_pack(_entry("agents", "a")))
        assert (cache.agents_dir / "notes.txt").exists()

    def test_a_stray_file_in_the_skills_directory_is_kept_and_a_stray_directory_is_not(
        self, cache: GroupPolicyCache
    ):
        cache.materialize(_pack(_entry("skills", "s")))
        skills = cache.layout.dir_for("skills")
        (skills / "stray.txt").write_text("kept")
        (skills / "old-skill").mkdir()
        cache.materialize(_pack(_entry("skills", "s")))
        assert (skills / "stray.txt").exists()
        assert not (skills / "old-skill").exists()
        assert (skills / "s" / "SKILL.md").exists()

    def test_a_plugin_without_a_source_is_its_legacy_yaml_file_and_is_pruned_when_dropped(
        self, cache: GroupPolicyCache
    ):
        cache.materialize(_pack(_entry("plugins", "old", "name: old")))
        plugins = cache.layout.dir_for("plugins")
        assert (plugins / "old.yaml").read_text() == "name: old"
        cache.materialize(_pack(_entry("agents", "a")))
        assert not (plugins / "old.yaml").exists()

    def test_the_hooks_file_is_never_pruned_by_the_directory_sweep(self, cache: GroupPolicyCache):
        hook = _entry("hooks", "h", json.dumps({"event": "Stop", "command": "x"}))
        cache.materialize(_pack(hook))
        assert cache.layout.hooks_file.is_file()


class TestKindRegistry:
    def _registry(self) -> KindRegistry:
        return KindRegistry.default(
            PluginInstallCoordinator(GroupPolicyLayout(root=Path("/nowhere")))
        )

    def test_the_directories_a_pack_owns_exclude_extensions(self):
        assert self._registry().file_directories() == [
            "agents",
            "commands",
            "hooks",
            "mcps",
            "output-styles",
            "plugins",
            "rules",
            "scripts",
            "skills",
            "tools",
            "workflows",
        ]

    def test_an_unknown_kind_and_settings_are_not_kinds(self):
        registry = self._registry()
        assert registry.get("no-such-kind") is None
        assert registry.get("settings") is None
        assert registry.get("agents") is not None

    def test_hooks_and_extensions_are_never_pruned(self):
        pruned = {kind.name for kind in self._registry().pruned()}
        assert "hooks" not in pruned and "extensions" not in pruned
        assert {"agents", "skills", "scripts", "plugins", "mcps"} <= pruned
