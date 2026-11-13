"""``igni_core.extensions`` re-exports its public names and lists each in ``__all__``."""

from __future__ import annotations

import igni_core.extensions as extensions
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.registry import ExtensionRegistry
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.extensions.service import McpExtensionService


def test_every_name_in_all_resolves_and_none_is_listed_twice():
    assert len(extensions.__all__) == len(set(extensions.__all__))
    for name in extensions.__all__:
        assert hasattr(extensions, name), name


def test_the_star_import_brings_in_exactly_the_listed_names():
    namespace: dict[str, object] = {}
    exec("from igni_core.extensions import *", namespace)  # noqa: S102

    assert set(namespace) - {"__builtins__"} == set(extensions.__all__)


def test_the_listed_names_are_the_real_classes():
    assert extensions.ExtensionManifest is ExtensionManifest
    assert extensions.ExtensionRegistry is ExtensionRegistry
    assert extensions.ExtensionRpc is ExtensionRpc
    assert extensions.McpExtensionService is McpExtensionService
