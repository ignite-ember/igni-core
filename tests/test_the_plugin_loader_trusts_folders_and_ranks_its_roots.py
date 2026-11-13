"""Which roots the plugin loader reads, in what order, and what it hands on.

* A folder's own plugins (``.claude/plugins``, ``.igni/plugins``) load only once
  the trust service trusts the folder; the user's, the group's and the managed
  ones never wait for that. Without a service of its own the loader asks the
  process's.
* The group-policy root (``group-policy-igni``, under the data directory) ranks
  above project and user plugins and below the managed ones; equal ranks keep
  the first found.
* A plugin the group installed brings hooks marked as the group's.
* Plugins bundling a ``.lsp.json`` or a ``.monitors.json`` are listed by
  ``collect_lsp_roots`` / ``collect_monitor_roots``, skipping the disabled.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from igni_core.core.hooks.loader import HookLoader
from igni_core.core.hooks.registry import HookRegistry
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.plugins.loader import GROUP_POLICY_ROOT, PluginLoader
from igni_core.core.workspace_trust import WorkspaceTrust, WorkspaceTrustStore


def _plugin(root: Path, name: str, *, marker: str = "") -> Path:
    plugin_dir = root / name
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    manifest = {"name": name, "description": marker}
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest))
    return plugin_dir


class _RecordingTrust(WorkspaceTrust):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.asked: list[tuple[Path, str]] = []

    def is_trusted(self, project_dir, *, what: str = "") -> bool:
        self.asked.append((Path(project_dir), what))
        return super().is_trusted(project_dir, what=what)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A fake home, project, data directory and managed root (empty)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    managed = tmp_path / "managed"
    monkeypatch.setattr(
        "igni_core.core.plugins.loader._platform_managed_plugins_root", lambda: managed
    )
    return {
        "home": home,
        "project": tmp_path / "project",
        "data": tmp_path / "data",
        "managed": managed,
    }


@pytest.fixture
def trust(tmp_path: Path) -> _RecordingTrust:
    return _RecordingTrust(store=WorkspaceTrustStore(path=tmp_path / "trust.json"))


def _loaded(world, trust=None) -> PluginLoader:
    loader = PluginLoader(world["data"], trust=trust)
    loader.load_all(project_dir=world["project"])
    return loader


def _group_root(world) -> Path:
    return world["data"] / "group-policy" / "plugins"


class TestTheWorkspaceTrust:
    def test_an_untrusted_folder_s_plugins_do_not_load(self, world, trust):
        _plugin(world["project"] / ".claude" / "plugins", "from-claude")
        _plugin(world["project"] / CONFIG_DIR / "plugins", "from-igni")
        _plugin(world["home"] / CONFIG_DIR / "plugins", "personal")
        _plugin(_group_root(world), "from-group")
        _plugin(world["managed"] / CONFIG_DIR / "plugins", "from-managed")

        loader = _loaded(world, trust)

        assert [p.name for p in loader.list_plugins()] == ["from-group", "from-managed", "personal"]
        assert trust.asked == [(world["project"], "plugins")]

    def test_a_trusted_folder_s_plugins_load(self, world, trust):
        _plugin(world["project"] / ".claude" / "plugins", "from-claude")
        _plugin(world["project"] / CONFIG_DIR / "plugins", "from-igni")
        trust.trust(world["project"])

        loader = _loaded(world, trust)

        assert [p.name for p in loader.list_plugins()] == ["from-claude", "from-igni"]
        assert loader.get("from-igni").source.root == "project-igni"

    def test_a_folder_whose_trust_is_withdrawn_stops_loading_on_the_next_scan(self, world, trust):
        _plugin(world["project"] / CONFIG_DIR / "plugins", "mine")
        trust.trust(world["project"])
        assert _loaded(world, trust).get("mine") is not None

        trust.distrust(world["project"])

        assert _loaded(world, trust).get("mine") is None

    @pytest.mark.workspace_trust
    def test_without_a_service_the_process_s_decides(self, world):
        _plugin(world["project"] / CONFIG_DIR / "plugins", "mine")

        assert _loaded(world).get("mine") is None

    def test_a_process_that_trusts_the_folder_loads_its_plugins(self, world):
        _plugin(world["project"] / CONFIG_DIR / "plugins", "mine")

        assert _loaded(world).get("mine") is not None


class TestTheGroupPolicyRoot:
    def test_the_root_is_named_and_sits_under_the_data_directory(self, world):
        _plugin(_group_root(world), "org")

        plugin = _loaded(world).get("org")

        assert GROUP_POLICY_ROOT == "group-policy-igni"
        assert plugin.source.root == "group-policy-igni"
        assert plugin.source.priority == 5
        assert plugin.root_path == _group_root(world) / "org"
        assert not plugin.is_managed

    def test_it_beats_project_and_user_plugins_of_the_same_name(self, world, trust):
        _plugin(world["home"] / CONFIG_DIR / "plugins", "dup", marker="user")
        _plugin(world["project"] / CONFIG_DIR / "plugins", "dup", marker="project")
        _plugin(_group_root(world), "dup", marker="group")
        trust.trust(world["project"])

        assert _loaded(world, trust).get("dup").manifest.description == "group"

    def test_the_managed_root_beats_it(self, world):
        _plugin(_group_root(world), "dup", marker="group")
        _plugin(world["managed"] / ".claude" / "plugins", "dup", marker="managed")

        plugin = _loaded(world).get("dup")

        assert plugin.manifest.description == "managed"
        assert plugin.is_managed

    def test_a_second_scan_at_the_same_rank_keeps_the_plugin_found_first(self, world):
        plugin_dir = _plugin(world["home"] / CONFIG_DIR / "plugins", "dup", marker="first")
        loader = PluginLoader(world["data"])
        loader.load_all(project_dir=world["project"])
        manifest = plugin_dir / ".claude-plugin" / "plugin.json"
        manifest.write_text(json.dumps({"name": "dup", "description": "second"}))

        loader.load_all(project_dir=world["project"])

        assert loader.get("dup").manifest.description == "first"
        assert [p.name for p in loader.list_plugins()] == ["dup"]


def _hooks_plugin(root: Path, name: str) -> None:
    plugin_dir = _plugin(root, name)
    (plugin_dir / "hooks").mkdir()
    (plugin_dir / "hooks" / "hooks.json").write_text(
        json.dumps({"PreToolUse": [{"type": "command", "command": f"{name}-cmd"}]})
    )


class TestTheGroupsHooks:
    def test_only_a_group_installed_plugin_s_hooks_are_marked_from_the_group(self, world):
        _hooks_plugin(_group_root(world), "org")
        _hooks_plugin(world["home"] / CONFIG_DIR / "plugins", "mine")
        loader = _loaded(world)
        registry = HookRegistry.from_empty()

        loader.apply_to_hooks(HookLoader(project_dir=world["project"]), registry)

        marked = {h.command: h.from_group for h in registry.raw["PreToolUse"]}
        assert marked == {"org-cmd": True, "mine-cmd": False}


class TestLspAndMonitorRoots:
    def _bundle(self, root: Path, name: str, *, lsp: bool = False, monitors: bool = False) -> None:
        plugin_dir = _plugin(root, name)
        if lsp:
            (plugin_dir / ".lsp.json").write_text("{}")
        if monitors:
            (plugin_dir / ".monitors.json").write_text("{}")

    def test_the_inventory_flags_follow_the_files(self, world):
        root = world["home"] / CONFIG_DIR / "plugins"
        self._bundle(root, "both", lsp=True, monitors=True)
        self._bundle(root, "neither")

        loader = _loaded(world)

        assert (loader.get("both").has_lsp, loader.get("both").has_monitors) == (True, True)
        assert (loader.get("neither").has_lsp, loader.get("neither").has_monitors) == (False, False)

    def test_each_collector_lists_the_plugins_that_bundle_its_file(self, world):
        root = world["home"] / CONFIG_DIR / "plugins"
        self._bundle(root, "a-lsp", lsp=True)
        self._bundle(root, "b-monitor", monitors=True)
        self._bundle(root, "c-both", lsp=True, monitors=True)
        self._bundle(root, "d-none")
        loader = _loaded(world)

        assert loader.collect_lsp_roots() == [
            (root / "a-lsp", "a-lsp"),
            (root / "c-both", "c-both"),
        ]
        assert loader.collect_monitor_roots() == [
            (root / "b-monitor", "b-monitor"),
            (root / "c-both", "c-both"),
        ]

    def test_disabled_plugins_are_left_out_but_still_listed(self, world):
        root = world["home"] / CONFIG_DIR / "plugins"
        self._bundle(root, "a", lsp=True, monitors=True)
        self._bundle(root, "b", lsp=True, monitors=True)
        loader = _loaded(world)

        assert loader.collect_lsp_roots(disabled={"a"}) == [(root / "b", "b")]
        assert loader.collect_monitor_roots(disabled={"a"}) == [(root / "b", "b")]
        assert [p.name for p in loader.list_plugins()] == ["a", "b"]

    def test_nothing_is_collected_when_nothing_is_loaded(self, world):
        loader = _loaded(world)

        assert loader.collect_lsp_roots() == []
        assert loader.collect_monitor_roots() == []


class TestInspection:
    def test_get_answers_the_plugin_or_none(self, world):
        _plugin(world["home"] / CONFIG_DIR / "plugins", "here")
        loader = _loaded(world)

        assert loader.get("here").name == "here"
        assert loader.get("absent") is None

    def test_a_manifest_that_is_not_utf8_is_skipped_with_a_warning(self, world, caplog):
        root = world["home"] / CONFIG_DIR / "plugins"
        _plugin(root, "good")
        bad = _plugin(root, "bad")
        (bad / ".claude-plugin" / "plugin.json").write_bytes(b'{"name": "\xff\xfe"}')

        loader = _loaded(world)

        assert [p.name for p in loader.list_plugins()] == ["good"]
        assert "Failed to parse plugin manifest" in caplog.text

    def test_the_mcp_flag_accepts_either_file_name(self, world):
        root = world["home"] / CONFIG_DIR / "plugins"
        (_plugin(root, "dotted") / ".mcp.json").write_text("{}")
        (_plugin(root, "plain") / "mcp.json").write_text("{}")

        loader = _loaded(world)

        assert loader.get("dotted").has_mcp
        assert loader.get("plain").has_mcp
