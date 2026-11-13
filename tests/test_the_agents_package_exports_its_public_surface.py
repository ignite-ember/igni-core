"""``igni_core.core.agents`` is the one import path for the agent classes.

The old ``igni_core.core.pool`` shim is gone: callers import from the package.
"""

from __future__ import annotations

import importlib

import pytest

import igni_core.core.agents as agents

EXPECTED = {
    "AgentBuildContext": "igni_core.core.agents.schemas",
    "AgentBuilder": "igni_core.core.agents.builder",
    "AgentConstructorArgs": "igni_core.core.agents.schemas",
    "AgentContributionApplier": "igni_core.core.agents.contributions",
    "AgentDefinition": "igni_core.core.agents.schemas",
    "AgentDefinitionLoader": "igni_core.core.agents.loader",
    "AgentEntry": "igni_core.core.agents.schemas",
    "AgentInfo": "igni_core.core.agents.schemas",
    "AgentMarkdownFile": "igni_core.core.agents.markdown",
    "AgentPool": "igni_core.core.agents.pool",
    "AgentPriority": "igni_core.core.agents.schemas",
    "EphemeralAgentStore": "igni_core.core.agents.ephemeral",
    "LoadError": "igni_core.core.agents.schemas",
    "LoadReport": "igni_core.core.agents.schemas",
    "PluginRestrictionPolicy": "igni_core.core.agents.plugin_policy",
}


def test_the_package_exports_exactly_these_names() -> None:
    assert sorted(agents.__all__) == sorted(EXPECTED)


@pytest.mark.parametrize(("name", "module"), sorted(EXPECTED.items()))
def test_each_name_is_the_class_its_module_defines(name: str, module: str) -> None:
    assert getattr(agents, name) is getattr(importlib.import_module(module), name)


def test_the_old_pool_shim_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("igni_core.core.pool")
