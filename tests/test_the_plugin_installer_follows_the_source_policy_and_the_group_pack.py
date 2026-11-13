"""Install and update clone and fetch only from sources the source policy
allows, and the install URL is recorded for ``update`` to check. A group
pack's installer (``scoped_to``) installs into its own directory and records
no pins; ``release_installed_from`` hands back what an older release put among
the user's plugins. An update that cannot fetch says so and moves nothing.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from igni_core.core.plugins.git import GitClient, GitError
from igni_core.core.plugins.installer import PluginError, PluginInstaller
from igni_core.core.plugins.source_policy import PluginSourcePolicy
from igni_core.core.plugins.state import PluginsState, load_state, save_state
from tests.plugin_git_repos import commit_again, git, plugin_repo

pytestmark = pytest.mark.skipif(not GitClient().is_available(), reason="git not available")

AIR_GAPPED = PluginSourcePolicy(air_gapped=True)


def _installer(tmp_path: Path, **kwargs) -> PluginInstaller:
    return PluginInstaller(data_dir=tmp_path / "data", **kwargs)


class TestTheSourcePolicy:
    def test_an_install_from_outside_an_air_gapped_network_is_refused(self, tmp_path):
        installer = _installer(tmp_path, source_policy=AIR_GAPPED)

        with pytest.raises(PluginError, match="air-gapped"):
            installer.install("https://github.com/acme/plugin.git")

        assert not installer.plugins_dir.exists()
        assert not (tmp_path / "data" / "_plugin_install_tmp").exists()

    def test_the_install_url_is_recorded_as_the_plugin_s_source(self, tmp_path):
        remote = plugin_repo(tmp_path)
        installer = _installer(tmp_path)

        installer.install(str(remote))

        assert load_state(tmp_path / "data").sources == {"demo": str(remote)}
        assert installer.source_of("demo") == str(remote)

    def test_an_update_from_a_source_the_policy_refuses_fetches_nothing(self, tmp_path):
        remote = plugin_repo(tmp_path)
        _installer(tmp_path).install(str(remote))
        state = load_state(tmp_path / "data")
        state.sources["demo"] = "https://github.com/acme/demo.git"
        save_state(state, data_dir=tmp_path / "data")
        commit_again(remote)
        pinned = load_state(tmp_path / "data").pins["demo"]

        with pytest.raises(PluginError, match="Not updating plugin 'demo'.*air-gapped"):
            _installer(tmp_path, source_policy=AIR_GAPPED).update("demo")

        assert load_state(tmp_path / "data").pins["demo"] == pinned
        assert git(tmp_path / "data" / "plugins" / "demo", "rev-parse", "HEAD") == pinned

    def test_an_air_gapped_update_with_no_recorded_source_asks_for_a_reinstall(self, tmp_path):
        remote = plugin_repo(tmp_path)
        plugin_dir = tmp_path / "data" / "plugins" / "demo"
        plugin_dir.parent.mkdir(parents=True)
        shutil.copytree(remote, plugin_dir)
        assert load_state(tmp_path / "data").sources == {}

        with pytest.raises(PluginError, match="no recorded source, and igni is air-gapped"):
            _installer(tmp_path, source_policy=AIR_GAPPED).update("demo")

    def test_a_plugin_with_no_recorded_source_updates_from_its_git_origin(self, tmp_path):
        remote = plugin_repo(tmp_path)
        installer = _installer(tmp_path)
        installer.install(str(remote))
        state = load_state(tmp_path / "data")
        state.sources.clear()
        save_state(state, data_dir=tmp_path / "data")
        newest = commit_again(remote)

        assert installer.source_of("demo") == str(remote)
        assert installer.update("demo") == newest

    def test_a_plugin_with_neither_a_record_nor_an_origin_has_no_source(self, tmp_path):
        installer = _installer(tmp_path)
        (installer.plugins_dir / "loose").mkdir(parents=True)

        assert installer.source_of("loose") is None
        assert installer.source_of("absent") is None


class TestAnUpdateThatCannotFetch:
    def test_it_says_so_and_leaves_the_plugin_as_it_was(self, tmp_path):
        remote = plugin_repo(tmp_path)
        installer = _installer(tmp_path)
        installer.install(str(remote))
        pinned = load_state(tmp_path / "data").pins["demo"]
        shutil.rmtree(remote)

        with pytest.raises(PluginError, match="Failed to fetch updates for plugin 'demo'"):
            installer.update("demo")

        assert load_state(tmp_path / "data").pins["demo"] == pinned
        assert git(tmp_path / "data" / "plugins" / "demo", "rev-parse", "HEAD") == pinned

    def test_an_unknown_ref_is_git_s_own_error_and_moves_nothing(self, tmp_path):
        remote = plugin_repo(tmp_path)
        installer = _installer(tmp_path)
        installer.install(str(remote))
        pinned = load_state(tmp_path / "data").pins["demo"]

        with pytest.raises(GitError, match="no-such-ref"):
            installer.update("demo", ref="no-such-ref")

        assert load_state(tmp_path / "data").pins["demo"] == pinned

    def test_a_subdir_install_is_not_a_repository_and_cannot_update(self, tmp_path):
        remote = tmp_path / "mono"
        (remote / "pkg" / ".claude-plugin").mkdir(parents=True)
        (remote / "pkg" / ".claude-plugin" / "plugin.json").write_text('{"name": "sub"}')
        git(remote, "init", "-q", "-b", "main")
        git(remote, "add", "-A")
        git(remote, "commit", "-qm", "one")
        installer = _installer(tmp_path)

        installer.install(str(remote), subdir="pkg")

        assert load_state(tmp_path / "data").pins == {"sub": ""}
        with pytest.raises(PluginError, match="Failed to fetch updates for plugin 'sub'"):
            installer.update("sub")


class TestAGroupPacksInstaller:
    def test_it_installs_into_its_own_directory_under_the_entry_name_and_records_nothing(
        self, tmp_path
    ):
        remote = plugin_repo(tmp_path, manifest_name="inner")
        user = _installer(tmp_path)
        pack_dir = tmp_path / "data" / "group-policy" / "plugins"

        pack = user.scoped_to(pack_dir)
        manifest = pack.install(str(remote), directory_name="entry")

        assert manifest.name == "inner"
        assert pack.plugins_dir == pack_dir
        assert (pack_dir / "entry" / ".claude-plugin" / "plugin.json").is_file()
        assert not (user.plugins_dir / "entry").exists()
        assert not (tmp_path / "data" / "plugins.json").exists()

    def test_it_keeps_the_source_policy_of_the_installer_it_came_from(self, tmp_path):
        pack = _installer(tmp_path, source_policy=AIR_GAPPED).scoped_to(tmp_path / "pack")

        with pytest.raises(PluginError, match="air-gapped"):
            pack.install("https://github.com/acme/plugin.git")

    def test_release_forgets_what_an_older_release_installed_from_the_url(self, tmp_path):
        remote = plugin_repo(tmp_path)
        other = plugin_repo(tmp_path, "other")
        installer = _installer(tmp_path)
        installer.install(str(remote))
        installer.install(str(other))

        released = installer.release_installed_from(str(remote))

        assert released == [installer.plugins_dir / "demo"]
        state = load_state(tmp_path / "data")
        assert state.pins.keys() == {"other"}
        assert state.sources == {"other": str(other)}
        assert (installer.plugins_dir / "demo").is_dir()

    def test_release_of_an_unknown_url_changes_nothing(self, tmp_path):
        installer = _installer(tmp_path)
        assert installer.release_installed_from("https://example.com/x.git") == []
        assert not (tmp_path / "data" / "plugins.json").exists()

    def test_release_returns_only_directories_that_still_exist(self, tmp_path):
        remote = plugin_repo(tmp_path)
        installer = _installer(tmp_path)
        installer.install(str(remote))
        shutil.rmtree(installer.plugins_dir / "demo")

        assert installer.release_installed_from(str(remote)) == []
        assert load_state(tmp_path / "data") == PluginsState()
