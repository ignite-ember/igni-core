"""A fetched page's media type decides how its body is read, and media types are
case-insensitive (RFC 9110 §8.3.1): a server answering ``text/HTML`` sends HTML,
and the model is given its visible text, not its markup."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from igni_core.core.tools.web import WebTools


async def _fetch(content_type: str, body: str) -> str:
    response = MagicMock()
    response.text = body
    response.headers = {"content-type": content_type}
    response.raise_for_status = MagicMock()
    with patch("igni_core.core.tools.http_fetcher.httpx.AsyncClient") as client:
        instance = AsyncMock()
        instance.get = AsyncMock(return_value=response)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        client.return_value = instance
        return await WebTools().fetch_url("https://example.com")


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", ["text/HTML", "TEXT/HTML; charset=UTF-8", "text/html"])
async def test_html_in_any_case_is_given_as_its_visible_text(content_type):
    result = await _fetch(
        content_type, "<html><body><p>Hello</p><script>x()</script></body></html>"
    )

    assert "Hello" in result
    assert "<p>" not in result
    assert "x()" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", ["application/JSON", "Application/Json"])
async def test_json_in_any_case_is_given_as_it_came(content_type):
    body = '{"html": "<b>kept</b>"}'

    assert await _fetch(content_type, body) == body
