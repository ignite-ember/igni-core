"""The portal URL derived from ``api_url``, for the ways a person writes one.

``tests/test_no_vendor_endpoint_is_assumed.py`` pins the mapping for
well-formed URLs with a scheme. A URL typed without one (``api.example.com``,
``localhost:7777``) used to come back as ``https:`` or ``localhost:`` — the
host went missing, so sign-in opened a portal that could not be opened.
"""

from __future__ import annotations

import pytest

from igni_core.core.config.endpoint import portal_url_for


@pytest.mark.parametrize(
    ("api_url", "portal"),
    [
        ("api.example.com", "https://example.com"),
        ("dev-api.example.com/v1", "https://dev.example.com"),
        ("localhost:7777", "https://localhost:7777"),
        ("  api.example.com/  ", "https://example.com"),
        ("api", "https://api"),
        ("https://api", "https://api"),
        ("http://api.example.com:8080/v1?x=1#frag", "http://example.com:8080"),
        ("https://api-dev.example.com/", "https://dev.example.com"),
        ("https://API.example.com", "https://API.example.com"),
        ("http://127.0.0.1:7777///", "http://127.0.0.1:7777"),
    ],
)
def test_the_host_survives_however_the_url_was_written(api_url: str, portal: str) -> None:
    assert portal_url_for(api_url) == portal
