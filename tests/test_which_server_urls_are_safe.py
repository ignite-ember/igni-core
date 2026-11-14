"""The one rule for an address written down as the server."""

from __future__ import annotations

import pytest

from igni_core.core.auth.server_url import LOCAL_HOSTS, is_safe_url


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com",
        "https://api.example.com:8443/base",
        "HTTPS://API.EXAMPLE.COM",
        "https://10.0.0.5",
        "http://localhost",
        "http://localhost:8080",
        "http://127.0.0.1",
        "http://127.0.0.1:7777/x",
        "http://[::1]:7777",
        "HTTP://LOCALHOST:1",
    ],
)
def test_https_and_local_plaintext_are_safe(url):
    assert is_safe_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "",
        "example.com",
        "//example.com",
        "https:///path",
        "http://example.com",
        "http://localhost.evil.example",
        "http://127.0.0.1.evil.example",
        "http://localhost@evil.example/",
        "http://127.0.0.2",
        "http://0.0.0.0",
        "ftp://example.com",
        "file://localhost/etc/passwd",
        "javascript://localhost/%0aalert(1)",
        "ws://localhost",
        "http://[::1",
        "https://[bad",
    ],
)
def test_everything_else_is_refused_and_never_raises(url):
    assert is_safe_url(url) is False


def test_the_plaintext_hosts_are_the_local_ones():
    assert set(LOCAL_HOSTS) == {"localhost", "127.0.0.1", "::1", "[::1]"}
