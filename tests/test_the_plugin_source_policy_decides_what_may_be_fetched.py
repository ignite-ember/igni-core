"""The plugin source policy: what a marketplace or plugin may be fetched from.

Built from settings the way session start builds it, with the real
settings model and with the partial stubs the test suite hands sessions.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from igni_core.core.config.schemas import NetworkConfig, PluginsConfig
from igni_core.core.config.settings import Settings
from igni_core.core.plugins.marketplace_store import DEFAULT_MARKETPLACES
from igni_core.core.plugins.models import MarketplaceEntry
from igni_core.core.plugins.source_policy import PluginSourcePolicy

[(OFFICIAL_NAME, OFFICIAL_URL)] = DEFAULT_MARKETPLACES
CUSTOMER = "https://git.acme.internal/tools/marketplace.git"


def _entry(name: str = "m", url: str = "https://x/y", added_by: str | None = None):
    return MarketplaceEntry(name=name, url=url, added_by=added_by)  # type: ignore[arg-type]


# ── from_settings ──────────────────────────────────────────────────


def test_a_real_settings_object_is_read_in_full() -> None:
    settings = Settings(
        air_gapped=True,
        plugins=PluginsConfig(default_marketplace=True, marketplaces=[CUSTOMER]),
        network=NetworkConfig(internal_hosts=["*.corp.test"]),
    )
    policy = PluginSourcePolicy.from_settings(settings)
    assert policy.air_gapped and policy.default_marketplace
    assert policy.customer_marketplaces == [CUSTOMER]
    assert "*.corp.test" in policy.internal_hosts
    assert "git.acme.internal" in policy.internal_hosts


def test_a_settings_stub_with_no_plugins_block_means_the_defaults() -> None:
    policy = PluginSourcePolicy.from_settings(SimpleNamespace())
    assert policy == PluginSourcePolicy()
    assert policy.marketplaces_to_register() == []


@pytest.mark.parametrize("truthy", ["true", 1, "yes", None])
def test_only_a_literal_true_means_air_gapped_or_default_marketplace(truthy: object) -> None:
    stub = SimpleNamespace(
        air_gapped=truthy, plugins=SimpleNamespace(default_marketplace=truthy, marketplaces=None)
    )
    policy = PluginSourcePolicy.from_settings(stub)
    assert policy.air_gapped is False and policy.default_marketplace is False


def test_marketplaces_that_are_not_a_list_are_ignored() -> None:
    stub = SimpleNamespace(plugins=SimpleNamespace(marketplaces=CUSTOMER))
    assert PluginSourcePolicy.from_settings(stub).customer_marketplaces == []


# ── marketplaces_to_register ───────────────────────────────────────


def test_the_default_comes_first_and_only_when_opted_in_and_not_air_gapped() -> None:
    mine = ["https://a/b", "https://c/d"]
    base = PluginSourcePolicy(customer_marketplaces=mine)
    assert base.marketplaces_to_register() == mine
    on = base.model_copy(update={"default_marketplace": True})
    assert on.marketplaces_to_register() == [OFFICIAL_URL, *mine]
    sealed = on.model_copy(update={"air_gapped": True})
    assert sealed.marketplaces_to_register() == mine


# ── refreshes_at_start ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        OFFICIAL_URL,
        OFFICIAL_URL + "/",
        OFFICIAL_URL + ".git",
        OFFICIAL_URL + ".git/",
        "  " + OFFICIAL_URL + "  ",
    ],
)
def test_an_old_install_stops_refreshing_the_official_marketplace_by_any_spelling(
    url: str,
) -> None:
    policy = PluginSourcePolicy()
    assert policy.refreshes_at_start(_entry(name="renamed", url=url)) is False


def test_the_official_marketplace_is_recognised_by_name_too() -> None:
    policy = PluginSourcePolicy()
    assert policy.refreshes_at_start(_entry(name=OFFICIAL_NAME, url="https://mirror/x")) is False


def test_other_marketplaces_always_refresh_at_start() -> None:
    for policy in (PluginSourcePolicy(), PluginSourcePolicy(air_gapped=True)):
        assert policy.refreshes_at_start(_entry()) is True
        assert policy.refreshes_at_start(_entry(added_by="default")) is True


def test_a_person_who_added_the_official_marketplace_keeps_it_refreshing() -> None:
    entry = _entry(name=OFFICIAL_NAME, url=OFFICIAL_URL, added_by="user")
    assert PluginSourcePolicy().refreshes_at_start(entry) is True
    assert PluginSourcePolicy(air_gapped=True).refreshes_at_start(entry) is True


def test_opting_in_refreshes_the_official_marketplace_unless_air_gapped() -> None:
    entry = _entry(name=OFFICIAL_NAME, url=OFFICIAL_URL, added_by="default")
    assert PluginSourcePolicy(default_marketplace=True).refreshes_at_start(entry) is True
    sealed = PluginSourcePolicy(default_marketplace=True, air_gapped=True)
    assert sealed.refreshes_at_start(entry) is False


# ── allows / refusal ───────────────────────────────────────────────


def test_everything_is_allowed_when_not_air_gapped() -> None:
    assert PluginSourcePolicy().allows("https://anywhere.example/x")


def test_air_gapped_allows_local_sources_and_refuses_public_hosts() -> None:
    policy = PluginSourcePolicy(air_gapped=True)
    for local in ("/srv/git/m.git", "file:///srv/git/m.git", "./relative", "C:\\repos\\m"):
        assert policy.allows(local), local
    for remote in (OFFICIAL_URL, "git@github.com:o/r.git", "ssh://github.com/o/r"):
        assert not policy.allows(remote), remote


@pytest.mark.parametrize(
    "url",
    [CUSTOMER, CUSTOMER[:-4], CUSTOMER + "/", "  " + CUSTOMER + " ", CUSTOMER[:-4] + "/"],
)
def test_a_listed_marketplace_is_allowed_whatever_its_trailing_spelling(url: str) -> None:
    policy = PluginSourcePolicy(air_gapped=True, customer_marketplaces=[CUSTOMER])
    assert policy.allows(url)


def test_another_repository_on_a_listed_host_is_allowed_but_a_lookalike_host_is_not() -> None:
    policy = PluginSourcePolicy(air_gapped=True, customer_marketplaces=[CUSTOMER])
    assert policy.allows("https://GIT.ACME.INTERNAL/other/plugin")
    assert policy.allows("git@git.acme.internal:other/plugin.git")
    assert not policy.allows("https://git.acme.internal.evil.test/other")
    assert not policy.allows("https://acme.internal/other")


def test_internal_hosts_are_inside_for_plugins_as_for_everything_else() -> None:
    policy = PluginSourcePolicy(air_gapped=True, internal_hosts=["*.corp.test", "git.one.test"])
    assert policy.allows("https://x.corp.test/r")
    assert policy.allows("https://git.one.test/r")
    assert not policy.allows("https://corp.test.evil/r")
    assert not policy.allows("https://git.two.test/r")


def test_changing_a_public_field_changes_the_next_answer() -> None:
    policy = PluginSourcePolicy(air_gapped=True)
    assert not policy.allows("https://h.test/r")
    policy.customer_marketplaces.append("https://h.test/m")
    assert policy.allows("https://h.test/r")


def test_the_refusal_names_the_url_and_the_three_remedies() -> None:
    assert PluginSourcePolicy().refusal("https://h.test/r") == (
        "https://h.test/r is outside this network and igni is air-gapped "
        "(air_gapped: true). Use a local path, list the marketplace "
        "under plugins.marketplaces, or add its host to network.internal_hosts."
    )


def test_local_and_host_questions_are_answered_like_the_network_policy() -> None:
    assert PluginSourcePolicy.is_local("/a/b") and PluginSourcePolicy.is_local("file:///a")
    assert not PluginSourcePolicy.is_local("https://h/r")
    assert PluginSourcePolicy.host_of("https://GitHub.com/o/r") == "github.com"
    assert PluginSourcePolicy.host_of("git@Example.org:o/r") == "example.org"
    assert PluginSourcePolicy.host_of("/a/b") is None
