"""``PluginController``: what the plugins panel shows before and after an install.

Preview shallow-clones a plugin that is not installed, scans it, deletes the
clone and shows the source the person knows instead of the temporary path;
``list_installed`` and ``contents`` describe installed plugins; ``set_enabled``
persists a sorted disabled list. The loader, the plugin state and the scanner
are real; only git is a fake that writes a plugin into the clone directory.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from igni_core.backend.plugin_controller import PluginController
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.plugins.git import GitError
from igni_core.core.plugins.loader import PluginLoader
from igni_core.core.plugins.state import PluginsState, load_state
from igni_core.core.session.core import PluginReloadCounts


class FakeGit:
    """Stands in for ``GitClient``: records its clones, writes ``files`` into the destination."""

    available = True
    files: dict[str, str] = {
        "README.md": "# Cloned\n",
        "skills/s/SKILL.md": "---\ndescription: d\n---\n",
    }
    fail: str = ""
    clones: list[dict] = []
    seen_threads: list[threading.Thread] = []

    def is_available(self) -> bool:
        return FakeGit.available

    def clone(self, url: str, dest: Path, *, ref: str | None = None, shallow: bool = True) -> None:
        FakeGit.clones.append({"url": url, "dest": dest, "ref": ref, "shallow": shallow})
        FakeGit.seen_threads.append(threading.current_thread())
        if FakeGit.fail:
            raise GitError(FakeGit.fail)
        for rel, text in FakeGit.files.items():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)


def _reset_git(**overrides: object) -> None:
    FakeGit.available = True
    FakeGit.files = {"README.md": "# Cloned\n", "skills/s/SKILL.md": "---\ndescription: d\n---\n"}
    FakeGit.fail = ""
    FakeGit.clones = []
    FakeGit.seen_threads = []
    for key, value in overrides.items():
        setattr(FakeGit, key, value)


def _session(tmp_path: Path, **settings: object) -> SimpleNamespace:
    loader = PluginLoader()
    return SimpleNamespace(
        settings=SimpleNamespace(
            storage=SimpleNamespace(data_dir=str(tmp_path / "data")), **settings
        ),
        plugin_loader=loader,
        plugin_state=PluginsState(),
        reloads=0,
        reload_plugins=lambda: PluginReloadCounts(plugins=0, skills=0, agents=0, hooks=0),
    )


def _plant(root: Path, name: str, **manifest: str) -> Path:
    plugin = root / name
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name, **manifest}))
    return plugin


def _load(session: SimpleNamespace, tmp_path: Path, *, managed: Path | None = None) -> None:
    with (
        patch.object(Path, "home", return_value=tmp_path / "home"),
        patch("igni_core.core.plugins.loader._platform_managed_plugins_root", lambda: managed),
    ):
        session.plugin_loader.load_all(project_dir=tmp_path / "proj")


async def _preview(session: SimpleNamespace, source: str, **kw: object):
    controller = PluginController(session)
    with patch("igni_core.backend.plugin_controller.GitClient", FakeGit):
        return controller, await controller.preview(source, **kw)


# ── preview ────────────────────────────────────────────────────────


async def test_a_preview_scans_the_clone_and_shows_the_source_not_the_temp_path(tmp_path):
    _reset_git()

    _, result = await _preview(_session(tmp_path), "https://example.com/p.git", branch="dev")

    assert result.error == ""
    assert [s.name for s in result.skills] == ["s"]
    assert result.readme == "# Cloned\n"
    assert result.root_path == "https://example.com/p.git"
    assert result.preview_source is not None
    assert (result.preview_source.url, result.preview_source.subdir) == (
        "https://example.com/p.git",
        "",
    )
    (clone,) = FakeGit.clones
    assert (clone["url"], clone["ref"], clone["shallow"]) == (
        "https://example.com/p.git",
        "dev",
        True,
    )


async def test_a_preview_deletes_its_clone_and_names_it_after_igni(tmp_path):
    _reset_git()

    await _preview(_session(tmp_path), "https://example.com/p.git")

    (clone,) = FakeGit.clones
    assert clone["dest"].name.startswith("igni-preview-")
    assert not clone["dest"].exists()


async def test_a_preview_clone_runs_off_the_event_loop(tmp_path):
    _reset_git()

    await _preview(_session(tmp_path), "https://example.com/p.git")

    assert FakeGit.seen_threads[0] is not threading.current_thread()


async def test_a_second_preview_of_the_same_source_is_served_without_cloning(tmp_path):
    _reset_git()
    session = _session(tmp_path)
    controller, first = await _preview(session, "https://example.com/p.git", branch="dev")
    with patch("igni_core.backend.plugin_controller.GitClient", FakeGit):
        again = await controller.preview("https://example.com/p.git", branch="dev")
        other_branch = await controller.preview("https://example.com/p.git", branch="main")

    assert again is first
    assert other_branch is not first
    assert len(FakeGit.clones) == 2


async def test_a_preview_of_a_subdirectory_scans_only_that_subdirectory(tmp_path):
    _reset_git(
        files={"README.md": "root", "plugins/one/README.md": "inner", "plugins/one/tools/t.py": ""}
    )

    _, result = await _preview(_session(tmp_path), "https://example.com/m.git [plugins/one]")

    assert result.readme == "inner"
    assert [t.name for t in result.tools] == ["t"]
    assert result.root_path == "https://example.com/m.git [plugins/one]"
    assert FakeGit.clones[0]["url"] == "https://example.com/m.git"


async def test_an_explicit_subdirectory_beats_the_one_in_the_source(tmp_path):
    _reset_git(files={"a/README.md": "a", "b/README.md": "b"})

    _, result = await _preview(_session(tmp_path), "https://example.com/m.git [a]", subdir="b")

    assert result.readme == "b"


async def test_a_preview_of_a_missing_subdirectory_says_the_entry_may_be_stale(tmp_path):
    _reset_git()

    _, result = await _preview(_session(tmp_path), "https://example.com/m.git [gone]")

    assert "no 'gone' subdirectory" in result.error
    assert not FakeGit.clones[0]["dest"].exists()


async def test_a_failed_clone_is_an_error_result_and_leaves_no_clone_behind(tmp_path):
    _reset_git(fail="repository not found")

    controller, result = await _preview(_session(tmp_path), "https://example.com/p.git")

    assert result.error == "git clone failed: repository not found"
    assert not FakeGit.clones[0]["dest"].exists()
    # A failure is not cached: asking again clones again.
    _reset_git()
    with patch("igni_core.backend.plugin_controller.GitClient", FakeGit):
        retry = await controller.preview("https://example.com/p.git")
    assert retry.error == ""


async def test_a_machine_without_git_gets_an_error_result(tmp_path):
    _reset_git(available=False)

    _, result = await _preview(_session(tmp_path), "https://example.com/p.git")

    assert result.error == "git is not installed on this machine."
    assert FakeGit.clones == []


async def test_a_source_the_policy_forbids_is_refused_before_any_clone(tmp_path):
    _reset_git()

    _, result = await _preview(_session(tmp_path, air_gapped=True), "https://example.com/p.git")

    assert result.error != ""
    assert FakeGit.clones == []


async def test_a_scan_that_fails_unexpectedly_is_an_error_result_not_an_exception(tmp_path):
    _reset_git()
    controller = PluginController(_session(tmp_path))

    with (
        patch("igni_core.backend.plugin_controller.GitClient", FakeGit),
        patch(
            "igni_core.backend.plugin_controller.PluginDirectoryScanner.scan",
            side_effect=RuntimeError("scan blew up"),
        ),
    ):
        result = await controller.preview("https://example.com/p.git")

    assert result.error == "scan blew up"
    assert not FakeGit.clones[0]["dest"].exists()


# ── list_installed and contents ────────────────────────────────────


def test_the_installed_list_describes_each_plugin_with_its_flags_and_pin(tmp_path):
    session = _session(tmp_path)
    user = tmp_path / "home" / CONFIG_DIR / "plugins"
    rich = _plant(user, "rich", version="1.2.0", description="Does things")
    (rich / "skills").mkdir()
    (rich / "hooks").mkdir()
    (rich / "hooks" / "hooks.json").write_text("{}")
    _plant(user, "bare")
    session.plugin_state = PluginsState(disabled=["bare"], pins={"rich": "abc123"})
    _load(session, tmp_path)

    listed = {p.name: p for p in PluginController(session).list_installed()}

    rich_info = listed["rich"]
    assert (rich_info.version, rich_info.description, rich_info.pin) == (
        "1.2.0",
        "Does things",
        "abc123",
    )
    assert rich_info.enabled is True
    assert (rich_info.has_skills, rich_info.has_hooks, rich_info.has_agents) == (True, True, False)
    assert Path(rich_info.path) == rich
    bare = listed["bare"]
    assert (bare.version, bare.description, bare.pin, bare.enabled) == ("", "", "", False)


def test_a_managed_plugin_is_listed_enabled_even_when_the_saved_list_disables_it(tmp_path):
    session = _session(tmp_path)
    managed = tmp_path / "managed"
    _plant(managed / CONFIG_DIR / "plugins", "org-policy")
    session.plugin_state = PluginsState(disabled=["org-policy"])
    _load(session, tmp_path, managed=managed)

    (info,) = PluginController(session).list_installed()

    assert (info.managed, info.enabled) == (True, True)


def test_the_contents_of_an_installed_plugin_are_read_from_its_directory(tmp_path):
    session = _session(tmp_path)
    plugin = _plant(tmp_path / "home" / CONFIG_DIR / "plugins", "alpha")
    (plugin / "README.md").write_text("# Alpha")
    _load(session, tmp_path)

    contents = PluginController(session).contents("alpha")

    assert (contents.name, contents.readme, contents.error) == ("alpha", "# Alpha", "")
    assert Path(contents.root_path) == plugin


def test_the_contents_of_an_unknown_plugin_are_an_error(tmp_path):
    session = _session(tmp_path)
    _load(session, tmp_path)

    assert PluginController(session).contents("ghost").error == "Plugin 'ghost' not found"


# ── set_enabled ────────────────────────────────────────────────────


def test_disabling_saves_a_sorted_list_and_reloads_the_session(tmp_path):
    session = _session(tmp_path)
    user = tmp_path / "home" / CONFIG_DIR / "plugins"
    for name in ("zeta", "alpha", "mid"):
        _plant(user, name)
    _load(session, tmp_path)
    reloads: list[int] = []
    session.reload_plugins = lambda: (
        reloads.append(1) or PluginReloadCounts(plugins=0, skills=0, agents=0, hooks=0)
    )
    controller = PluginController(session)

    controller.set_enabled("zeta", False)
    controller.set_enabled("alpha", False)
    controller.set_enabled("alpha", False)

    assert load_state(data_dir=tmp_path / "data").disabled == ["alpha", "zeta"]
    assert len(reloads) == 3


def test_enabling_removes_only_that_plugin_from_the_saved_list(tmp_path):
    session = _session(tmp_path)
    user = tmp_path / "home" / CONFIG_DIR / "plugins"
    _plant(user, "a")
    _plant(user, "b")
    session.plugin_state = PluginsState(disabled=["a", "b"])
    _load(session, tmp_path)

    result = PluginController(session).set_enabled("a", True)

    assert "enabled" in result.text and "MCP servers are starting" in result.text
    assert load_state(data_dir=tmp_path / "data").disabled == ["b"]


def test_a_managed_plugin_cannot_be_disabled_and_nothing_is_saved_or_reloaded(tmp_path):
    session = _session(tmp_path)
    managed = tmp_path / "managed"
    _plant(managed / CONFIG_DIR / "plugins", "org-policy")
    _load(session, tmp_path, managed=managed)
    session.reload_plugins = lambda: (_ for _ in ()).throw(AssertionError("reloaded"))

    result = PluginController(session).set_enabled("org-policy", False)

    assert "managed" in result.text and "cannot be disabled" in result.text
    assert load_state(data_dir=tmp_path / "data").disabled == []


def test_an_unknown_plugin_cannot_be_toggled(tmp_path):
    session = _session(tmp_path)
    _load(session, tmp_path)

    result = PluginController(session).set_enabled("ghost", True)

    assert result.text == "No plugin named 'ghost'."
    assert load_state(data_dir=tmp_path / "data").disabled == []
