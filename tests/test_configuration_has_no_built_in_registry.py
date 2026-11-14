"""docs/CONFIGURATION.md does not lean on a built-in model registry.

The file says "There is no built-in registry" — and further down still
listed "Is <name> in built-in registry? → use it" as a resolution step
and said "the built-in registry handles everything". The resolver
(``ModelRegistry._resolve_entry``) has two steps: a ``models.registry``
row, else a ``provider:model_id`` name.
"""

from __future__ import annotations

import pathlib

from igni_core.core.config.models import ModelRegistry
from igni_core.core.config.settings import Settings

_DOC = pathlib.Path(__file__).resolve().parent.parent / "docs" / "CONFIGURATION.md"


def test_built_in_registry_appears_only_where_the_doc_says_there_is_none():
    lines = [
        line
        for line in _DOC.read_text(encoding="utf-8").splitlines()
        if "built-in registry" in line
    ]

    assert lines == ['**There is no built-in registry.** This section said "Built-in registry']


def test_a_name_neither_in_the_registry_nor_provider_qualified_resolves_to_nothing():
    registry = ModelRegistry(Settings())

    assert registry._resolve_entry("MiniMax-M2.7") is None
    assert registry._resolve_entry("openai:gpt-4o").model_id == "gpt-4o"
