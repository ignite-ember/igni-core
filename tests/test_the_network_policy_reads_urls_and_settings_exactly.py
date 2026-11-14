"""``NetworkPolicy``'s parsing edges: what counts as local, which host a URL
names, how hosts compare, what settings contribute, and the refusal text.

The policy's decisions through its consumers are in
``test_an_air_gapped_install_reaches_only_its_network.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from igni_core.core.network_policy import NetworkPolicy


def _gapped(*hosts: str) -> NetworkPolicy:
    return NetworkPolicy(air_gapped=True, internal_hosts=list(hosts))


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://Git.Acme.Internal/repo.git", "git.acme.internal"),
        ("https://user:pw@git.acme.internal:8443/x", "git.acme.internal"),
        ("http://[::1]:8080/x", "::1"),
        ("git@github.com:org/repo.git", "github.com"),
        ("github.com:org/repo", "github.com"),
        ("  https://a.example/x  ", "a.example"),
    ],
)
def test_the_host_a_remote_url_names(url, host):
    assert NetworkPolicy.host_of(url) == host


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "/srv/repos/x",
        "./x",
        "file:///srv/x",
        "FILE:///srv/x",
        "C:\\repos\\x",
        "C:/repos/x",
    ],
)
def test_a_local_target_names_no_host_and_is_local(url):
    assert NetworkPolicy.host_of(url) is None
    assert NetworkPolicy.is_local(url)


def test_a_malformed_url_names_no_host_and_is_not_allowed_under_air_gap():
    url = "http://[::1/x"  # unbalanced bracket: urlsplit raises ValueError
    assert NetworkPolicy.host_of(url) is None
    assert _gapped().allows(url) is False


def test_a_url_without_a_host_is_refused_under_air_gap():
    assert _gapped("acme.internal").allows("http:///x") is False


def test_hosts_compare_without_case_brackets_or_the_root_dot():
    policy = _gapped("Git.Acme.Internal.", "[fd00::1]")
    assert policy.allows("https://git.acme.internal/x")
    assert policy.allows("https://GIT.ACME.INTERNAL./x")
    assert policy.is_inside("fd00::1")
    assert NetworkPolicy.is_loopback("LOCALHOST.")
    assert NetworkPolicy.is_loopback("[::1]")
    assert NetworkPolicy.is_loopback("127.4.5.6")
    assert not NetworkPolicy.is_loopback("128.0.0.1")


def test_a_suffix_pattern_covers_subdomains_only():
    policy = _gapped("*.acme.internal")
    assert policy.is_inside("a.b.acme.internal")
    assert not policy.is_inside("acme.internal")
    assert not policy.is_inside("evil-acme.internal")


def test_without_air_gap_everything_is_allowed():
    assert NetworkPolicy().allows("https://anywhere.example/x")


def test_the_refusal_names_the_host_the_remedy_and_what_was_not_contacted():
    message = _gapped().refusal("https://git.outside.example/x.git", what="the clone")
    assert "https://git.outside.example/x.git is outside this network" in message
    assert "the clone was not contacted" in message
    assert "If git.outside.example is inside your network" in message
    assert "network.internal_hosts" in message


def test_the_refusal_falls_back_to_the_url_when_it_names_no_host():
    message = _gapped().refusal("http:///x")
    assert "If http:///x is inside" in message
    assert "it was not contacted" in message


def test_settings_contribute_the_hosts_they_configure():
    settings = SimpleNamespace(
        air_gapped=True,
        network=SimpleNamespace(internal_hosts=["Extra.Internal", 7, None, "*.corp.internal"]),
        plugins=SimpleNamespace(marketplaces=["https://market.acme.internal/x", 3, "/local/dir"]),
        api_url="https://api.acme.internal",
        portal_url="https://portal.acme.internal/",
        update_endpoint="https://updates.acme.internal/v1",
    )
    policy = NetworkPolicy.from_settings(settings)
    assert policy.air_gapped is True
    assert sorted(policy.internal_hosts) == sorted(
        [
            "Extra.Internal",
            "*.corp.internal",
            "market.acme.internal",
            "api.acme.internal",
            "portal.acme.internal",
            "updates.acme.internal",
        ]
    )


def test_a_partial_settings_object_is_not_air_gapped_and_adds_no_hosts():
    policy = NetworkPolicy.from_settings(SimpleNamespace())
    assert policy == NetworkPolicy(air_gapped=False, internal_hosts=[])


@pytest.mark.parametrize("value", ["yes", 1, "true", None])
def test_only_a_literal_true_air_gaps(value):
    assert NetworkPolicy.from_settings(SimpleNamespace(air_gapped=value)).air_gapped is False


def test_a_marketplaces_value_that_is_not_a_list_is_ignored():
    settings = SimpleNamespace(plugins=SimpleNamespace(marketplaces="https://m.acme.internal"))
    assert NetworkPolicy.from_settings(settings).internal_hosts == []
