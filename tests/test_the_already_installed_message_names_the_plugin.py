"""The "already installed" message names the plugin in the commands it suggests."""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.plugins.git import GitClient
from igni_core.core.plugins.installer import PluginError, PluginInstaller
from igni_core.core.plugins.source_policy import PluginSourcePolicy
from tests.plugin_git_repos import plugin_repo

pytestmark = pytest.mark.skipif(not GitClient().is_available(), reason="git not available")

AIR_GAPPED = PluginSourcePolicy(air_gapped=True)


def _installer(tmp_path: Path, **kwargs) -> PluginInstaller:
    return PluginInstaller(data_dir=tmp_path / "data", **kwargs)


class TestTheAlreadyInstalledMessage:
    def test_it_names_the_plugin_in_the_commands_it_suggests(self, tmp_path):
        remote = plugin_repo(tmp_path, "gamma")
        installer = _installer(tmp_path)
        installer.install(str(remote))

        with pytest.raises(PluginError) as raised:
            installer.install(str(remote))

        message = str(raised.value)
        assert "`/plugin remove gamma`" in message
        assert "`/plugin update gamma`" in message
        assert "{" not in message
