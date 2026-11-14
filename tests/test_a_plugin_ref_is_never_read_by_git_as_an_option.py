"""A ref git would read as an option (``--upload-pack=<cmd>`` runs a command) is
refused before git is run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.plugins.git import GitClient, GitError
from igni_core.core.plugins.installer import PluginError, PluginInstaller
from igni_core.core.plugins.source_policy import PluginSourcePolicy
from tests.plugin_git_repos import plugin_repo

pytestmark = pytest.mark.skipif(not GitClient().is_available(), reason="git not available")

AIR_GAPPED = PluginSourcePolicy(air_gapped=True)


def _installer(tmp_path: Path, **kwargs) -> PluginInstaller:
    return PluginInstaller(data_dir=tmp_path / "data", **kwargs)


class TestRefsGitWouldReadAsOptions:
    def test_an_install_ref_that_is_an_option_is_refused(self, tmp_path):
        remote = plugin_repo(tmp_path)
        installer = _installer(tmp_path)

        with pytest.raises(PluginError, match="cannot start with '-'"):
            installer.install(str(remote), ref="--upload-pack=true")

        assert not installer.plugins_dir.exists()

    def test_an_update_ref_that_is_an_option_runs_nothing(self, tmp_path):
        remote = plugin_repo(tmp_path)
        installer = _installer(tmp_path)
        installer.install(str(remote))
        marker = tmp_path / "ran"

        with pytest.raises(PluginError, match="cannot start with '-'"):
            installer.update("demo", ref=f"--upload-pack=touch {marker}")

        assert not marker.exists()

    @pytest.mark.parametrize("call", ["fetch_ref", "reset_hard", "checkout"])
    def test_the_git_client_refuses_an_option_as_a_ref(self, tmp_path, call):
        remote = plugin_repo(tmp_path)
        marker = tmp_path / "ran"

        with pytest.raises(GitError, match="cannot start with '-'"):
            getattr(GitClient(), call)(remote, f"--upload-pack=touch {marker}")

        assert not marker.exists()

    def test_the_git_client_refuses_an_option_as_a_clone_ref(self, tmp_path):
        remote = plugin_repo(tmp_path)

        with pytest.raises(GitError, match="cannot start with '-'"):
            GitClient().clone(str(remote), tmp_path / "dest", ref="--upload-pack=true")

        assert not (tmp_path / "dest").exists()
