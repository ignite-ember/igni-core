"""Defaults of the three small blocks that keep igni inside its sandbox and network."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.core.config.schemas import NetworkConfig, PluginsConfig
from igni_core.core.config.schemas.extensions import ExtensionsConfig


def test_extensions_run_sandboxed_and_do_not_require_a_sandbox_by_default():
    config = ExtensionsConfig()
    assert config.sandbox is True
    assert config.require_sandbox is False


def test_extension_switches_are_booleans_a_string_is_refused():
    with pytest.raises(ValidationError):
        ExtensionsConfig(sandbox="maybe")  # type: ignore[arg-type]
    assert ExtensionsConfig(sandbox=False).sandbox is False


def test_no_host_counts_as_internal_by_default_and_each_block_has_its_own_list():
    first, second = NetworkConfig(), NetworkConfig()
    assert first.internal_hosts == []
    first.internal_hosts.append("git.acme.internal")
    assert second.internal_hosts == []


def test_the_official_marketplace_is_off_and_no_organisation_marketplace_is_listed():
    config = PluginsConfig()
    assert config.default_marketplace is False
    assert config.marketplaces == []


def test_plugin_marketplace_lists_are_not_shared_between_blocks():
    first, second = PluginsConfig(), PluginsConfig()
    first.marketplaces.append("/srv/plugins")
    assert second.marketplaces == []
