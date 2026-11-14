"""Install, update and remove act inside the plugins directory: a subdir, a
directory name, a manifest name or a plugin name that is a path is refused
rather than followed out of it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from igni_core.core.plugins.git import GitClient
from igni_core.core.plugins.installer import PluginError, PluginInstaller
from igni_core.core.plugins.source_policy import PluginSourcePolicy
from igni_core.core.plugins.state import load_state
from tests.plugin_git_repos import plugin_repo

pytestmark = pytest.mark.skipif(not GitClient().is_available(), reason="git not available")

AIR_GAPPED = PluginSourcePolicy(air_gapped=True)


def _installer(tmp_path: Path, **kwargs) -> PluginInstaller:
    return PluginInstaller(data_dir=tmp_path / "data", **kwargs)


class TestStayingInsideThePluginsDirectory:
    @pytest.mark.parametrize("subdir", ["../escape", "a/../../escape", "/etc", "..", "a\\..\\b"])
    def test_a_subdir_that_leaves_the_clone_is_refused(self, tmp_path, subdir):
        remote = plugin_repo(tmp_path)
        installer = _installer(tmp_path)

        with pytest.raises(PluginError, match="relative path inside the repo"):
            installer.install(str(remote), subdir=subdir)

        assert not installer.plugins_dir.exists()
        assert load_state(tmp_path / "data").pins == {}

    @pytest.mark.parametrize("name", ["../escape", "a/b", "..", ".", "a\\b"])
    def test_a_directory_name_that_is_a_path_is_refused(self, tmp_path, name):
        remote = plugin_repo(tmp_path)
        installer = _installer(tmp_path)

        with pytest.raises(PluginError, match="single name"):
            installer.install(str(remote), directory_name=name)

        assert not (tmp_path / "escape").exists()
        assert not installer.plugins_dir.exists()

    def test_a_manifest_name_that_is_a_path_is_refused_and_leaves_no_clone(self, tmp_path):
        remote = plugin_repo(tmp_path, "source", manifest_name="../escape")
        installer = _installer(tmp_path)

        with pytest.raises(PluginError, match="single name"):
            installer.install(str(remote))

        assert not (tmp_path / "data" / "escape").exists()
        assert not (tmp_path / "data" / "_plugin_install_tmp").exists()
        assert load_state(tmp_path / "data").pins == {}

    def test_a_manifest_name_that_is_a_path_installs_under_a_directory_name(self, tmp_path):
        remote = plugin_repo(tmp_path, "source", manifest_name="../escape")
        installer = _installer(tmp_path)

        manifest = installer.install(str(remote), directory_name="pack-entry")

        assert manifest.name == "../escape"
        assert (installer.plugins_dir / "pack-entry" / ".claude-plugin").is_dir()

    def test_remove_cannot_delete_outside_the_plugins_directory(self, tmp_path):
        installer = _installer(tmp_path)
        victim = tmp_path / "data" / "victim"
        victim.mkdir(parents=True)
        installer.plugins_dir.mkdir(parents=True)

        with pytest.raises(PluginError, match="single name"):
            installer.remove("../victim")

        assert victim.is_dir()

    def test_update_cannot_act_outside_the_plugins_directory(self, tmp_path):
        remote = plugin_repo(tmp_path)
        installer = _installer(tmp_path)
        installer.install(str(remote))
        shutil.copytree(installer.plugins_dir / "demo", tmp_path / "data" / "copy")

        with pytest.raises(PluginError, match="single name"):
            installer.update("../copy")
