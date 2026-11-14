"""The friendly-name resolver builds its reverse index once, on first use.

``Bash`` in a rule means ``run_shell_command`` and whatever else the catalog
names ``Bash``. The index is owned by the instance, built from an injectable
provider when first asked, and dropped by ``reset``. Constructing an
evaluator must not load the catalog (it sits on the CLI startup path).
"""

from __future__ import annotations

from igni_core.core.config.permission_eval.resolver import FriendlyToolNameResolver
from igni_core.protocol.tool_friendly_names import ToolFriendlyNames


class _Provider:
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self.calls = 0

    def __call__(self) -> dict[str, str]:
        self.calls += 1
        return dict(self.mapping)


def test_constructing_a_resolver_does_not_load_the_names():
    provider = _Provider({"a": "A"})
    FriendlyToolNameResolver(provider)
    assert provider.calls == 0


def test_the_names_are_loaded_once_across_lookups():
    provider = _Provider({"a": "A", "b": "B"})
    resolver = FriendlyToolNameResolver(provider)
    resolver.internals_for("A")
    resolver.internals_for("B")
    resolver.internals_for("nope")
    assert provider.calls == 1


def test_several_internal_names_behind_one_friendly_name_are_all_returned():
    resolver = FriendlyToolNameResolver(_Provider({"x1": "Run", "x2": "Run", "y": "Other"}))
    assert resolver.internals_for("Run") == frozenset({"x1", "x2"})
    assert resolver.internals_for("Other") == frozenset({"y"})


def test_an_unknown_empty_or_differently_cased_name_has_no_internals():
    resolver = FriendlyToolNameResolver(_Provider({"x": "Run"}))
    assert resolver.internals_for("Missing") == frozenset()
    assert resolver.internals_for("") == frozenset()
    assert resolver.internals_for("run") == frozenset()


def test_an_empty_mapping_yields_nothing():
    assert FriendlyToolNameResolver(_Provider({})).internals_for("Run") == frozenset()


def test_a_changed_provider_is_stale_until_reset():
    provider = _Provider({"x": "Run"})
    resolver = FriendlyToolNameResolver(provider)
    assert resolver.internals_for("Run") == frozenset({"x"})
    provider.mapping = {"z": "Run"}
    assert resolver.internals_for("Run") == frozenset({"x"})
    resolver.reset()
    assert resolver.internals_for("Run") == frozenset({"z"})
    assert provider.calls == 2


def test_two_resolvers_do_not_share_an_index():
    one = FriendlyToolNameResolver(_Provider({"x": "Run"}))
    two = FriendlyToolNameResolver(_Provider({"y": "Run"}))
    assert one.internals_for("Run") == frozenset({"x"})
    assert two.internals_for("Run") == frozenset({"y"})


def test_without_a_provider_the_catalog_is_used():
    resolver = FriendlyToolNameResolver()
    assert "run_shell_command" in resolver.internals_for("Bash")
    for internal, friendly in ToolFriendlyNames.all_names().items():
        assert internal in resolver.internals_for(friendly)


def test_the_default_resolver_is_one_shared_instance():
    assert FriendlyToolNameResolver.default() is FriendlyToolNameResolver.default()
