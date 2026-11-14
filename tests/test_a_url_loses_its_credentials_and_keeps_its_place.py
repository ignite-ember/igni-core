"""``url_without_credentials`` keeps where a URL points and drops what could unlock it."""

from __future__ import annotations

import pytest

from igni_core.protocol.redaction import url_without_credentials


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("https://user:secret@host.example.com/p", "https://host.example.com/p"),
        ("https://user@host.example.com/p", "https://host.example.com/p"),
        ("https://host.example.com/p?key=secret&x=1", "https://host.example.com/p"),
        ("https://u:p@host.example.com:8443/a/b?token=t", "https://host.example.com:8443/a/b"),
        ("https://host.example.com/p#frag", "https://host.example.com/p"),
        ("HTTPS://Host.Example.COM/Path", "https://host.example.com/Path"),
        ("https://host.example.com", "https://host.example.com"),
    ],
)
def test_the_user_password_query_and_fragment_are_dropped(url, shown):
    assert url_without_credentials(url) == shown


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("http://[::1]:8080/p", "http://[::1]:8080/p"),
        ("http://user:pw@[2001:db8::1]/p?k=v", "http://[2001:db8::1]/p"),
    ],
)
def test_an_ipv6_host_keeps_its_brackets(url, shown):
    assert url_without_credentials(url) == shown


@pytest.mark.parametrize("url", ["https://host:99999999/p", "http://[::1/p", "http://u:p@h:x/"])
def test_a_url_that_cannot_be_parsed_is_empty_not_an_error(url):
    assert url_without_credentials(url) == ""


@pytest.mark.parametrize("value", ["", "not a url", "/a/path?key=1", "just-text"])
def test_a_value_that_is_not_a_url_comes_back_as_it_was(value):
    assert url_without_credentials(value) == value
