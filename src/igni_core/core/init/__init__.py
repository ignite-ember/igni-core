"""Project init package — thin re-export layer over the class split.

The old single-file :mod:`igni_core.core.init` module was replaced
by an OOP-first package: :class:`ProjectInitializer` owns the flow,
:class:`InitConfig` owns the defaults, :class:`HookProvisioner` /
:class:`HomeConfigMigrator` own the two subflows, and the shipped
hooks live in :data:`BUILT_IN_HOOKS`.

This :mod:`__init__` re-exports the public class surface and keeps
the legacy ``initialize_project(project_dir) -> bool`` free function
as a 1-line shim over :meth:`ProjectInitializer.initialize` for the
two callers that still take the bool return shape
(``session/core.py`` migrates to the classmethod at call sites and
``tests/test_onboarding_and_audit.py`` keeps using the shim).
"""

from __future__ import annotations

from pathlib import Path

from igni_core.core.hooks.schemas import HookDefinition
from igni_core.core.init.home_migrator import HomeConfigMigrator
from igni_core.core.init.hook_provisioner import HookProvisioner
from igni_core.core.init.hooks_catalog import BUILT_IN_HOOKS
from igni_core.core.init.project_initializer import ProjectInitializer
from igni_core.core.init.schemas import (
    BuiltInHookSpec,
    InitConfig,
    InitResult,
    SettingsFile,
)


def initialize_project(project_dir: Path, **config_kwargs) -> bool:
    """Compat shim — delegates to :meth:`ProjectInitializer.initialize`.

    Returns ``True`` if this run performed first-time init on this
    project. ``**config_kwargs`` are forwarded as :class:`InitConfig`
    fields (``package_dir=`` is the common one for tests).
    """
    return ProjectInitializer.initialize(project_dir, **config_kwargs)


__all__ = [
    "BUILT_IN_HOOKS",
    "BuiltInHookSpec",
    "HomeConfigMigrator",
    "HookDefinition",
    "HookProvisioner",
    "InitConfig",
    "InitResult",
    "ProjectInitializer",
    "SettingsFile",
    "initialize_project",
]
