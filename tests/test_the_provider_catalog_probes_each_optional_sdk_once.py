"""Which providers a catalog offers, and when it looks for the optional ones.

``openai_like`` is always there. ``gemini`` and ``anthropic`` need an SDK, so
they are looked for on first use, offered only when it imports, and not looked
for again when it did not.
"""

from __future__ import annotations

import sys

import pytest

from igni_core.core.config import provider_catalog
from igni_core.core.config.provider_builders import (
    AnthropicBuilder,
    GeminiBuilder,
    OpenAILikeBuilder,
)
from igni_core.core.config.provider_catalog import ProviderCatalog


@pytest.fixture
def without_sdks(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "igni_core.core.config.checked_gemini", None)
    monkeypatch.setitem(sys.modules, "igni_core.core.config.logging_claude", None)


class TestWithTheSdksInstalled:
    def test_a_new_catalog_offers_all_three_sorted(self):
        assert ProviderCatalog().available_providers() == ["anthropic", "gemini", "openai_like"]

    def test_each_name_has_its_own_builder_type(self):
        catalog = ProviderCatalog()

        assert isinstance(catalog.builder_for("openai_like"), OpenAILikeBuilder)
        assert isinstance(catalog.builder_for("gemini"), GeminiBuilder)
        assert isinstance(catalog.builder_for("anthropic"), AnthropicBuilder)

    def test_an_unknown_or_empty_name_is_not_a_provider(self):
        catalog = ProviderCatalog()

        for name in ("nope", "", "OPENAI_LIKE"):
            assert catalog.has(name) is False
            assert catalog.builder_for(name) is None

    def test_the_builder_is_the_same_one_on_every_lookup(self):
        catalog = ProviderCatalog()

        assert catalog.builder_for("gemini") is catalog.builder_for("gemini")

    def test_two_catalogs_share_nothing(self):
        first, second = ProviderCatalog(), ProviderCatalog()
        first.register("custom", OpenAILikeBuilder())

        assert second.has("custom") is False
        assert second.builder_for("openai_like") is not first.builder_for("openai_like")


class TestRegister:
    def test_a_custom_builder_is_offered_and_listed(self):
        catalog = ProviderCatalog()
        custom = OpenAILikeBuilder()

        catalog.register("zeta", custom)

        assert catalog.has("zeta") is True
        assert catalog.builder_for("zeta") is custom
        assert catalog.available_providers() == ["anthropic", "gemini", "openai_like", "zeta"]

    def test_it_replaces_a_built_in_and_a_lazy_name(self):
        catalog = ProviderCatalog()
        mine, other = OpenAILikeBuilder(), OpenAILikeBuilder()

        catalog.register("openai_like", mine)
        catalog.register("gemini", other)

        assert catalog.builder_for("openai_like") is mine
        # Registered before the probe: the lazy gemini never takes its place.
        assert catalog.builder_for("gemini") is other
        assert catalog.available_providers() == ["anthropic", "gemini", "openai_like"]
        assert catalog.builder_for("gemini") is other


class TestWithoutTheSdks:
    def test_only_openai_like_is_offered(self, without_sdks):
        catalog = ProviderCatalog()

        assert catalog.available_providers() == ["openai_like"]
        assert catalog.has("gemini") is False
        assert catalog.has("anthropic") is False
        assert catalog.builder_for("gemini") is None
        assert catalog.builder_for("anthropic") is None

    def test_a_missing_sdk_is_probed_once(self, without_sdks, monkeypatch: pytest.MonkeyPatch):
        built: list[str] = []

        class Counting(GeminiBuilder):
            def __init__(self) -> None:
                built.append("gemini")
                super().__init__()

        monkeypatch.setitem(provider_catalog.ProviderCatalog._LAZY, "gemini", Counting)
        catalog = ProviderCatalog()

        catalog.has("gemini")
        catalog.builder_for("gemini")
        catalog.available_providers()
        catalog.available_providers()

        assert built == ["gemini"]

    def test_an_sdk_that_appears_later_is_not_picked_up_by_that_catalog(
        self, without_sdks, monkeypatch: pytest.MonkeyPatch
    ):
        catalog = ProviderCatalog()
        assert catalog.has("anthropic") is False

        monkeypatch.delitem(sys.modules, "igni_core.core.config.logging_claude")

        assert catalog.has("anthropic") is False
        assert ProviderCatalog().has("anthropic") is True
