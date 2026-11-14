"""``igni_core.extensions.sandbox`` is where callers import the sandbox's names from."""

from __future__ import annotations

import importlib

import pytest

import igni_core.extensions.sandbox as package

EXPORTED = {
    "NO_PROXY": "launcher",
    "REQUIRE_SANDBOX_ENV": "settings",
    "SANDBOX_ENV": "launcher",
    "Confinement": "launcher",
    "EgressProxy": "proxy",
    "ExtensionSandbox": "launcher",
    "HostAllowList": "proxy",
    "MechanismDeps": "mechanism",
    "MechanismName": "mechanism",
    "MechanismRun": "mechanism",
    "SandboxAvailability": "mechanism",
    "SandboxMechanism": "mechanism",
    "SandboxPlan": "plan",
    "SandboxRefused": "launcher",
    "SandboxSettings": "settings",
}


def test_the_package_exports_exactly_these_names():
    assert sorted(package.__all__) == sorted(EXPORTED)


@pytest.mark.parametrize("name", sorted(EXPORTED))
def test_each_exported_name_is_the_one_its_module_defines(name):
    module = importlib.import_module(f"igni_core.extensions.sandbox.{EXPORTED[name]}")
    assert getattr(package, name) is getattr(module, name)


def test_the_platform_sandboxes_are_reached_by_their_modules_not_the_package():
    for module in ("appcontainer", "bubblewrap", "seatbelt", "winapi", "winbridge", "bridge"):
        assert module not in package.__all__
        importlib.import_module(f"igni_core.extensions.sandbox.{module}")
