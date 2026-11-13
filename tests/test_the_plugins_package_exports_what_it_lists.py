"""``igni_core.core.plugins`` re-exports its public names and lists each in ``__all__``."""

from __future__ import annotations

import igni_core.core.plugins as plugins
from igni_core.core.plugins.git import GitClient, GitError
from igni_core.core.plugins.installer import PluginError, PluginInstaller
from igni_core.core.plugins.loader import PluginLoader


def test_every_name_in_all_resolves_and_none_is_listed_twice():
    assert len(plugins.__all__) == len(set(plugins.__all__))
    for name in plugins.__all__:
        assert hasattr(plugins, name), name


def test_the_star_import_brings_in_exactly_the_listed_names():
    namespace: dict[str, object] = {}
    exec("from igni_core.core.plugins import *", namespace)  # noqa: S102

    assert set(namespace) - {"__builtins__"} == set(plugins.__all__)


def test_the_installer_and_loader_names_are_the_real_classes():
    assert plugins.GitClient is GitClient
    assert plugins.GitError is GitError
    assert plugins.PluginError is PluginError
    assert plugins.PluginInstaller is PluginInstaller
    assert plugins.PluginLoader is PluginLoader


def test_the_package_exposes_the_state_helpers_and_default_marketplaces():
    assert callable(plugins.load_state)
    assert callable(plugins.save_state)
    assert plugins.DEFAULT_MARKETPLACES
