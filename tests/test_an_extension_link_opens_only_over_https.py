"""``ExternalLink``: the one shape of link an extension may ask igni to open.

Absolute ``https`` with a host, no credentials, no whitespace or control
characters, at most 2048 characters, and no other field.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.extensions.host.external_link import ExternalLink


class TestAcceptedLinks:
    @pytest.mark.parametrize(
        "url",
        [
            "https://code.example.com/repos/42?tab=commits#top",
            "HTTPS://code.example.com/",
            "https://code.example.com:8443/path",
            "https://[::1]/x",
        ],
    )
    def test_an_absolute_https_link_with_a_host_is_kept_as_written(self, url: str) -> None:
        assert ExternalLink(url=url).url == url

    def test_two_thousand_and_forty_eight_characters_are_the_longest(self) -> None:
        stem = "https://example.com/"
        assert len(ExternalLink(url=stem + "a" * (2048 - len(stem))).url) == 2048
        with pytest.raises(ValidationError):
            ExternalLink(url=stem + "a" * (2049 - len(stem)))


class TestRefusedLinks:
    @pytest.mark.parametrize(
        ("url", "why"),
        [
            ("http://example.com/", "only https links open outside igni, not 'http'"),
            ("javascript:alert(1)", "only https links open outside igni, not 'javascript'"),
            ("file:///etc/passwd", "only https links open outside igni, not 'file'"),
            ("/relative/path", "only https links open outside igni, not '/relative/path'"),
            ("https:///no-host", "a link names its host"),
            ("https://user:secret@example.com/", "a link carries no credentials"),
            ("https://user@example.com/", "a link carries no credentials"),
            ("https://:secret@example.com/", "a link carries no credentials"),
            ("https://example.com/a b", "no whitespace or control characters"),
            ("https://example.com/a\tb", "no whitespace or control characters"),
            ("https://example.com/a\nb", "no whitespace or control characters"),
            ("https://example.com/a\x00b", "no whitespace or control characters"),
            ("https://example.com/a\x1bb", "no whitespace or control characters"),
        ],
    )
    def test_a_link_that_is_not_safe_to_open_is_refused_with_why(self, url: str, why: str) -> None:
        with pytest.raises(ValidationError) as caught:
            ExternalLink(url=url)
        assert why in str(caught.value)

    def test_nothing_but_the_url_is_taken(self) -> None:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            ExternalLink.model_validate({"url": "https://example.com/", "target": "_blank"})
